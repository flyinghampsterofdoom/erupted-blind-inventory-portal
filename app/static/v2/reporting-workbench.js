(() => {
  const root = document.querySelector('[data-reporting-workbench]');
  if (!root) return;

  const picker = root.querySelector('[data-saved-view-picker]');
  picker?.addEventListener('change', () => {
    window.location.assign(picker.value ? `/v2/reports?saved_view_id=${encodeURIComponent(picker.value)}` : '/v2/reports');
  });

  const form = root.querySelector('[data-report-form]');
  const reportType = root.querySelector('[data-report-type]');
  const dateMode = root.querySelector('[data-date-mode]');
  const syncControls = () => {
    const type = reportType.value;
    root.querySelectorAll('[data-sales-control]').forEach((node) => { node.hidden = type !== 'sales_analysis'; });
    root.querySelectorAll('[data-stock-control]').forEach((node) => { node.hidden = type !== 'stock_value'; });
    root.querySelectorAll('[data-replenishment-control]').forEach((node) => { node.hidden = type !== 'replenishment'; });
    root.querySelectorAll('[data-analysis-control]').forEach((node) => { node.hidden = type === 'replenishment'; });
    root.querySelectorAll('[data-date-control]').forEach((node) => { node.hidden = !['sales_analysis', 'replenishment'].includes(type); });
    root.querySelectorAll('[data-custom-date]').forEach((node) => {
      node.hidden = !['sales_analysis', 'replenishment'].includes(type) || !['custom', 'choose_when_run'].includes(dateMode.value);
    });
    root.querySelectorAll('select[data-report-option]').forEach((select) => {
      const options = [...select.options];
      options.forEach((option) => { option.hidden = !['both', type].includes(option.dataset.for); });
      if (select.selectedOptions[0]?.hidden) select.value = options.find((option) => !option.hidden)?.value || '';
    });
  };
  reportType?.addEventListener('change', syncControls);
  dateMode?.addEventListener('change', syncControls);
  syncControls();

  root.querySelectorAll('[data-token-editor]').forEach((editor) => {
    const input = editor.querySelector('[data-token-input]');
    const chips = editor.querySelector('[data-token-chips]');
    const hiddenName = editor.dataset.tokenName;
    const addTerms = (raw) => {
      const existing = new Set([...chips.querySelectorAll('[data-term]')].map((node) => node.dataset.term.toLocaleLowerCase()));
      raw.split(/[,;\n\r]+/).map((value) => value.trim()).filter(Boolean).forEach((term) => {
        const key = term.toLocaleLowerCase();
        if (existing.has(key)) return;
        existing.add(key);
        const chip = document.createElement('span');
        chip.className = `reporting-chip${hiddenName === 'exclude_term' ? ' reporting-chip--exclude' : ''}`;
        chip.dataset.term = term;
        chip.append(document.createTextNode(term));
        const remove = document.createElement('button');
        remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', `Remove ${term}`);
        const hidden = document.createElement('input');
        hidden.type = 'hidden'; hidden.name = hiddenName; hidden.value = term;
        chip.append(remove, hidden); chips.append(chip);
      });
    };
    editor.addEventListener('click', (event) => {
      const button = event.target.closest('.reporting-chip button');
      if (button) button.closest('.reporting-chip').remove();
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ',' || event.key === ';') {
        event.preventDefault(); addTerms(input.value); input.value = '';
      }
    });
    input.addEventListener('input', () => {
      if (/[,;\n\r]/.test(input.value)) { addTerms(input.value); input.value = ''; }
    });
    input.addEventListener('blur', () => { addTerms(input.value); input.value = ''; });
  });

  form?.addEventListener('submit', () => {
    root.querySelectorAll('[data-token-input]').forEach((input) => input.dispatchEvent(new Event('blur')));
  });

  const showExcluded = root.querySelector('[data-show-replenishment-excluded]');
  const syncReplenishmentRows = () => {
    const rows = [...root.querySelectorAll('[data-replenishment-row]')];
    rows.forEach((row) => {
      const manual = row.querySelector('[name="manual_exclusion"]')?.checked || false;
      const excluded = row.dataset.autoExcluded === 'true' || manual;
      row.dataset.excluded = excluded ? 'true' : 'false';
      row.hidden = excluded && !showExcluded?.checked;
    });
    const excludedCount = rows.filter((row) => row.dataset.excluded === 'true').length;
    const shown = root.querySelector('[data-replenishment-shown]');
    const excluded = root.querySelector('[data-replenishment-excluded]');
    if (shown) shown.textContent = String(rows.length - excludedCount);
    if (excluded) excluded.textContent = String(excludedCount);
  };
  showExcluded?.addEventListener('change', syncReplenishmentRows);
  root.querySelectorAll('[name="manual_exclusion"]').forEach((checkbox) => checkbox.addEventListener('change', syncReplenishmentRows));
  syncReplenishmentRows();

  const poMode = root.querySelector('[data-po-mode]');
  const syncPoMode = () => {
    const target = root.querySelector('[data-target-weeks]');
    if (target && poMode) target.hidden = poMode.value !== 'target_weeks';
  };
  poMode?.addEventListener('change', syncPoMode);
  syncPoMode();

  const updatePreviewCosts = () => {
    let total = 0;
    root.querySelectorAll('[data-po-preview-form] tbody tr').forEach((row) => {
      const quantity = row.querySelector('[data-final-qty]');
      const costCell = row.querySelector('[data-unit-cost]');
      const lineCost = row.querySelector('[data-line-cost]');
      const cost = Number(costCell?.dataset.unitCost);
      if (!quantity || !Number.isFinite(cost)) return;
      const amount = Math.max(0, Number(quantity.value) || 0) * cost;
      if (!quantity.disabled) total += amount;
      if (lineCost) lineCost.textContent = `$${amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    });
    const totalNode = root.querySelector('[data-preview-total]');
    if (totalNode) totalNode.textContent = total.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  root.querySelectorAll('[data-preview-include]').forEach((checkbox) => {
    checkbox.addEventListener('change', () => {
      const row = checkbox.closest('tr');
      const exclusion = row.querySelector('[data-preview-exclusion]');
      const quantity = row.querySelector('[data-final-qty]');
      exclusion.disabled = checkbox.checked;
      quantity.disabled = !checkbox.checked;
      row.classList.toggle('is-excluded', !checkbox.checked);
      updatePreviewCosts();
    });
  });
  root.querySelectorAll('[data-final-qty]').forEach((input) => input.addEventListener('input', updatePreviewCosts));

  root.querySelectorAll('[data-sortable-table]').forEach((table) => {
    table.querySelectorAll('[data-sort-column]').forEach((button) => button.addEventListener('click', () => {
      const index = Number(button.dataset.sortColumn);
      const ascending = button.dataset.direction !== 'asc';
      table.querySelectorAll('[data-sort-column]').forEach((other) => delete other.dataset.direction);
      button.dataset.direction = ascending ? 'asc' : 'desc';
      const rows = [...table.tBodies[0].rows];
      rows.sort((left, right) => {
        const a = left.cells[index]?.textContent.trim() || '';
        const b = right.cells[index]?.textContent.trim() || '';
        const aNumber = Number(a.replace(/[$,%]/g, '').replaceAll(',', ''));
        const bNumber = Number(b.replace(/[$,%]/g, '').replaceAll(',', ''));
        const comparison = Number.isFinite(aNumber) && Number.isFinite(bNumber)
          ? aNumber - bNumber : a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
        return ascending ? comparison : -comparison;
      });
      rows.forEach((row) => table.tBodies[0].append(row));
    }));
  });
})();
