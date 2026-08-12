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
    root.querySelectorAll('[data-date-control]').forEach((node) => { node.hidden = type !== 'sales_analysis'; });
    root.querySelectorAll('[data-custom-date]').forEach((node) => {
      node.hidden = type !== 'sales_analysis' || !['custom', 'choose_when_run'].includes(dateMode.value);
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
})();
