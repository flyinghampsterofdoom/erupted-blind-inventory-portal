/* Presentation only: move the original controls, keeping their handlers/forms. */
(() => {
  'use strict';
  const storeTones={'Andresen':'andresen','HWY 99':'hwy99','Longview':'longview','SR 503':'sr503'};
  const storeTone=name=>storeTones[name]||'neutral';
  // The current standard full-day assignment. Keep any different hours visible;
  // this label never changes the assignment or introduces a shift category.
  const shiftLabel=shift=>!shift.is_coverage && shift.start_time==='08:45' && shift.end_time==='22:00' ? 'Full Shift' : shift.time_label||'Hours unavailable';
  function closureDays(board) {
    return board.week.days.map(day => {
      const closed=(board.business_hours||[]).filter(item=>item.date===day.iso && item.closed);
      const all=board.stores.length>1 && board.stores.every(store=>closed.some(item=>item.store_name===store.name));
      const names=[...new Set(closed.map(item=>item.event_name).filter(Boolean))];
      return {date:day.iso,all,closed,label:all ? `${names.join(' / ')||'All stores'} · Closed` : closed.map(item=>`${item.store_name} closed`).join(' · ')};
    });
  }
  if (typeof module!=='undefined' && module.exports) module.exports={closureDays,storeTone,shiftLabel};
  if (typeof document==='undefined') return;
  const root=document.querySelector('[data-schedule-board]');
  if (!root) return;
  let board=JSON.parse(root.querySelector('[data-board-json]').textContent);
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text)n.textContent=text;return n;};
  const dialog=el('dialog','shift-inspector');
  dialog.setAttribute('aria-labelledby','shift-inspector-title');
  dialog.innerHTML='<header><h2 id="shift-inspector-title"></h2><button type="button" data-inspector-close aria-label="Close shift details">×</button></header><div data-inspector-facts></div><div data-inspector-context></div>';
  root.append(dialog);
  const title=dialog.querySelector('h2'), facts=dialog.querySelector('[data-inspector-facts]'), context=dialog.querySelector('[data-inspector-context]');
  let current=null, suppressClickUntil=0, pointer=null;
  const dateLabel=value=>new Intl.DateTimeFormat('en-US',{weekday:'long',year:'numeric',month:'long',day:'numeric',timeZone:'UTC'}).format(new Date(`${value}T12:00:00Z`));
  function restore() {
    if (!current) return;
    const {card,extras,trigger,add,cell}=current;
    if(add){add.textContent='+';cell.append(add);}
    extras.hidden=true; card.prepend(extras);
    context.removeAttribute('data-shift-card');context.removeAttribute('data-shift-id');
    context.querySelector('[data-assignment-shortcut]')?.remove();
    context.querySelector('[data-inspector-cell]')?.remove();
    trigger?.setAttribute('aria-expanded','false');
    current=null;
    if (!document.querySelector('dialog[open]') && trigger?.isConnected) trigger.focus({preventScroll:true});
  }
  dialog.addEventListener('close',restore);
  dialog.querySelector('[data-inspector-close]').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close();});
  function position(card) {
    dialog.style.removeProperty('left');dialog.style.removeProperty('top');
    if (matchMedia('(max-width:620px)').matches) return;
    const r=card.getBoundingClientRect(), d=dialog.getBoundingClientRect(), gap=8;
    dialog.style.left=`${Math.max(gap,Math.min(r.left,innerWidth-d.width-gap))}px`;
    dialog.style.top=`${Math.max(gap,Math.min(r.bottom+gap,innerHeight-d.height-gap))}px`;
  }
  function open(card,quick,trigger) {
    if (performance.now()<suppressClickUntil) return;
    if (dialog.open) { dialog.close(); restore(); }
    const shift=board.shifts.find(s=>s.id===Number(card.dataset.shiftId));
    if (!shift) return;
    const extras=card.querySelector('[data-shift-extras]');
    const cell=card.closest('[data-drop-cell]');
    const add=cell?.querySelector(':scope > [data-cell-add]');
    current={card,extras,trigger,cell,add};
    dialog.classList.toggle('is-quick',quick);
    title.textContent=quick ? `Shift actions · ${shift.employee_name||'Open shift'}` : 'Shift details';
    facts.replaceChildren();
    const dl=el('dl');
    for(const [label,value] of [['Employee',shift.employee_name||'Unassigned'],['Date',dateLabel(shift.shift_date)],['Store',shift.store_name],['Shift',shift.time_label],['Role',shift.is_coverage?'Coverage':shift.is_lead_of_day?'Company-wide Lead':shift.is_double_coverage?'Double Coverage':shift.shift_type_name||'Standard assignment']]) {
      dl.append(el('dt','',label),el('dd','',value));
    }
    if(shift.is_coverage)dl.append(el('dt','','Covering'),el('dd','',shift.covering_employee_name));
    else dl.append(el('dt','','Originally scheduled hours'),el('dd','',String(shift.original_scheduled_hours ?? shift.paid_hours)),el('dt','','Effective hours'),el('dd','',String(shift.effective_hours ?? shift.paid_hours)));
    dl.append(el('dt','','Actual hours'),el('dd','',shift.actual_hours == null ? 'Not established' : String(shift.actual_hours)));
    if(shift.manually_locked)dl.append(el('dt','','Lock'),el('dd','',shift.lock_reason||'Manually locked'));
    if(shift.lead_of_day_manually_assigned)dl.append(el('dt','','Lead'),el('dd','','Manager designation'));
    if(shift.employee_note)dl.append(el('dt','','Note'),el('dd','',shift.employee_note));
    facts.append(dl);
    const warnings=board.warnings.filter(w=>w.shift_id===shift.id || (w.employee_id===shift.employee_id && w.date===shift.shift_date) || (!w.employee_id && w.store_id===shift.store_id && w.date===shift.shift_date));
    if(warnings.length){const list=el('ul','shift-detail-warnings');warnings.forEach(w=>list.append(el('li','',`${w.severity}: ${w.message}`)));facts.append(list);}
    facts.hidden=quick;
    context.dataset.shiftCard='';context.dataset.shiftId=shift.id;
    extras.hidden=false;context.append(extras);
    extras.querySelectorAll('details.schedule-shift__menu').forEach(menu=>{menu.open=true;});
    // Attendance and Call Out are the first controls in both presentations.
    const operations=extras.querySelector('[aria-label="Attendance actions"]');
    if(operations)extras.prepend(operations);
    if(board.actions.swap && shift.employee_id && !shift.is_coverage) {
      const shortcut=el('button','', 'Change assignment');shortcut.type='button';shortcut.dataset.assignmentShortcut='';context.append(shortcut);
    }
    if(add){
      const cellContext=el('div');cellContext.dataset.inspectorCell='';cellContext.dataset.dropCell='';
      for(const key of ['employeeId','storeId','shiftDate'])cellContext.dataset[key]=cell.dataset[key];
      add.textContent='Add shift';cellContext.append(add);context.append(cellContext);
    }
    trigger.setAttribute('aria-expanded','true');
    dialog.showModal();position(card);
    if(quick) (context.querySelector('button')||dialog.querySelector('button')).focus();
  }
  function decorate() {
    root.querySelectorAll('[data-employee-row]').forEach(row=>{
      const count=row.querySelector('.schedule-employee__shift-count');
      if(!count)return;
      count.dataset.employeeIssues=row.dataset.employeeRow;
      count.setAttribute('role','button');count.tabIndex=0;
      count.setAttribute('aria-label',`Inspect allocation warnings for ${row.querySelector('.schedule-employee strong').textContent}: ${count.textContent.trim()}`);
    });
    // Existing warning links focus their target after scrolling. Day headers
    // need programmatic focus without adding another stop to the Tab order.
    board.warnings.forEach(warning=>{
      const target=document.getElementById(warning.target_id);
      if(target && !target.hasAttribute('tabindex'))target.tabIndex=-1;
    });
    root.classList.toggle('is-published-grid',board.period?.status==='PUBLISHED');
    root.querySelectorAll('.schedule-board-region [data-shift-card]:not(.is-compact)').forEach(card=>{
      const shift=board.shifts.find(s=>s.id===Number(card.dataset.shiftId));if(!shift)return;
      const extras=el('div','shift-original-controls');extras.dataset.shiftExtras='';extras.hidden=true;
      extras.append(...card.childNodes);card.append(extras);card.classList.add('is-compact');card.tabIndex=-1;
      card.dataset.storeTone=storeTone(shift.store_name);
      const main=el('div','shift-card-main');main.dataset.shiftDetailsOpen='';main.tabIndex=0;main.setAttribute('role','button');main.setAttribute('aria-haspopup','dialog');main.setAttribute('aria-expanded','false');
      main.setAttribute('aria-label',`Shift details: ${shift.employee_name||'Unassigned'}, ${shift.time_label}, ${shift.store_name}, ${dateLabel(shift.shift_date)}`);
      const line=el('span','shift-card-store');
      line.append(el('strong','shift-card-store-name',shift.store_name));
      if(shift.is_coverage)line.append(el('b','schedule-coverage-badge','COVERAGE'));
      if(shift.is_lead_of_day)line.append(el('b','shift-card-lead','Lead'));
      if(shift.manually_locked){const lock=el('span','','🔒');lock.setAttribute('aria-label','Manually locked');line.append(lock);}
      if(shift.has_warning){const warn=el('span','schedule-warning-symbol','▲');warn.setAttribute('aria-label','Shift has warning');line.append(warn);}
      if(!shift.is_coverage && (shift.exception?.status || (shift.attendance_statuses||[]).length)){
        const exception=el('span','shift-card-exception','!');
        const label=`Assignment exception: ${shift.exception?.status||(shift.attendance_statuses||[]).join(', ')}`;
        exception.setAttribute('aria-label',label);exception.title=label;line.append(exception);
      }
      if(shift.is_open)line.append(el('span','shift-card-open','Open'));
      main.append(line);
      main.append(el('small','shift-card-label',shiftLabel(shift)));
      if(shift.is_coverage)main.append(el('small','shift-card-covering',`Covering: ${shift.covering_employee_name}`));
      const quick=el('button','shift-card-quick','⋮');quick.type='button';quick.dataset.shiftQuick='';quick.setAttribute('aria-label',`Actions for ${shift.employee_name||'open shift'}, ${shift.time_label} at ${shift.store_name}`);quick.setAttribute('aria-haspopup','dialog');quick.setAttribute('aria-expanded','false');
      quick.hidden=!extras.querySelector('button');
      card.append(main);
      if(shift.can_record_attendance){
        const callout=el('button','shift-card-callout',shift.is_coverage?'Manage coverage':'Call-out / coverage');
        callout.type='button';callout.dataset.calloutOpen='';
        callout.setAttribute('aria-label',`${shift.is_coverage?'Manage coverage':'Record call-out or coverage'} for ${shift.employee_name}, ${shift.store_name}, ${dateLabel(shift.shift_date)}`);
        card.append(callout);
      }
      card.append(quick);
    });
    root.querySelectorAll('[data-closure-label]').forEach(node=>node.remove());
    root.querySelectorAll('.is-business-closed').forEach(node=>node.classList.remove('is-business-closed'));
    for(const day of closureDays(board)) {
      if(!day.closed.length)continue;
      const head=document.getElementById(`schedule-day-${day.date}`);
      if(head){const label=el('small','schedule-closure-label',day.label);label.dataset.closureLabel='';head.append(label);head.classList.toggle('is-business-closed',day.all);}
      if(day.all) root.querySelectorAll(`[data-drop-cell][data-shift-date="${day.date}"]`).forEach(cell=>cell.classList.add('is-business-closed'));
      else root.querySelectorAll(`[data-drop-cell][data-shift-date="${day.date}"]`).forEach(cell=>{
        const store=board.stores.find(s=>s.id===Number(cell.dataset.storeId));
        if(store && day.closed.some(item=>item.store_name===store.name))cell.classList.add('is-business-closed');
      });
    }
  }
  function employeeIssues(trigger) {
    const employee=board.employees.find(e=>e.id===Number(trigger.dataset.employeeIssues));
    if(!employee)return;
    const toggle=root.querySelector('[data-tool-toggle="warnings"]');
    if(!toggle)return;
    if(root.querySelector('[data-tool-panel="warnings"]').hidden)toggle.click();
    root.querySelector('[data-warning-filter-button="all"]').click();
    const list=root.querySelector('[data-warning-list]');
    const warnings=board.warnings.filter(w=>w.employee_id===employee.id);
    list.querySelectorAll('[data-warning-severity]').forEach((item,index)=>{item.hidden=board.warnings[index].employee_id!==employee.id;});
    const note=el('p','employee-warning-context',`${employee.name} · ${employee.assigned_shift_count} / ${employee.target_shift_count} shifts. ${warnings.length?'Showing this employee’s recorded warnings.':'No employee-specific warning is recorded; the allocation difference has not been reclassified.'} Choose All to view every issue.`);
    note.dataset.employeeWarningContext='';list.before(note);
    root.querySelector('[data-tool-panel="warnings"]').focus();
  }
  root.addEventListener('click',event=>{
    const employeeFilter=root.querySelector('[data-employee-warning-context]');
    if(employeeFilter && event.target.closest('[data-tool-toggle="warnings"],[data-open-warnings]'))root.querySelector('[data-warning-filter-button="all"]').click();
    if(event.target.closest('[data-warning-filter-button],[data-tool-toggle="warnings"],[data-open-warnings]'))employeeFilter?.remove();
    const allocation=event.target.closest('[data-employee-issues]');
    if(allocation){event.stopPropagation();employeeIssues(allocation);return;}
    if(dialog.contains(event.target)) {
      const shortcut=event.target.closest('[data-assignment-shortcut]');
      if(shortcut){event.stopPropagation();const id=context.dataset.shiftId;dialog.close();root.querySelector('[data-swap-open="REPLACE"]').click();root.querySelector('[data-swap-form]').elements.shift_id.value=id;return;}
      // Close before the existing handler opens its own dialog. Keep the actual
      // control in this shift context until that handler has consumed the click.
      const button=event.target.closest('button');
      if(button && !button.hasAttribute('data-inspector-close') && !button.closest('form'))dialog.close();
      return;
    }
    const card=event.target.closest('.is-compact[data-shift-card]');if(!card)return;
    const quick=event.target.closest('[data-shift-quick]');
    if(quick){event.stopPropagation();open(card,true,quick);return;}
    if(event.target.closest('button,form,input,select,a,summary'))return;
    event.stopPropagation();open(card,false,card.querySelector('[data-shift-details-open]'));
  },true);
  root.addEventListener('keydown',event=>{
    const allocation=event.target.closest('[data-employee-issues]');
    if(allocation && (event.key==='Enter'||event.key===' ')){event.preventDefault();event.stopPropagation();employeeIssues(allocation);return;}
    const main=event.target.closest('[data-shift-details-open]');
    const card=event.target.matches('.is-compact[data-shift-card]')?event.target:main?.closest('[data-shift-card]');
    if(card && (event.key==='Enter'||event.key===' ')){event.preventDefault();event.stopPropagation();open(card,false,main||card.querySelector('[data-shift-details-open]'));}
  },true);
  root.addEventListener('pointerdown',event=>{if(event.target.closest('.is-compact'))pointer={x:event.clientX,y:event.clientY};},true);
  root.addEventListener('pointerup',event=>{if(pointer && Math.hypot(event.clientX-pointer.x,event.clientY-pointer.y)>=8)suppressClickUntil=performance.now()+350;pointer=null;},true);
  root.addEventListener('pointercancel',()=>{pointer=null;},true);
  root.addEventListener('schedule:updated',event=>{if(dialog.open){dialog.close();restore();}board=event.detail?.board||board;});
  root.addEventListener('schedule:rendered',decorate);
  window.addEventListener('resize',()=>{if(dialog.open)position(current.card);});
  decorate();
})();
