(()=>{'use strict';const root=document.querySelector('#sku-classifications');if(!root)return;
root.querySelectorAll('[data-classification-form]').forEach(form=>{
 const scope=form.elements.scope,start=form.elements.start_date,end=form.elements.end_date,state=form.querySelector('[data-row-state]'),button=form.querySelector('button');
 const today=start.value;
 scope.addEventListener('change',()=>{const historical=scope.value==='HISTORICAL';form.querySelector('[data-end-date]').hidden=!historical;end.required=historical;if(historical){start.removeAttribute('min');start.max=today;end.value=start.value;}else{start.removeAttribute('max');start.min=today;start.value=today;end.value='';}});
 form.addEventListener('submit',async event=>{event.preventDefault();if(!form.reportValidity())return;button.disabled=true;
 const payload=Object.fromEntries(new FormData(form));payload.version=Number(payload.version);payload.end_date=payload.end_date||null;
 try{const response=await fetch('/management/purchasing/classifications',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf},body:JSON.stringify(payload)});const result=await response.json();if(!response.ok||result.saved!==true)throw Error(result.detail||'Save was not acknowledged; retry the same change.');state.textContent='Classification saved and audited. Refreshing the effective-date view…';const url=new URL(location.href);url.searchParams.set('as_of',payload.start_date);url.searchParams.set('saved','1');location.href=url;}
 catch(error){state.textContent=error.message;}finally{button.disabled=false;}
 });
});
if(new URL(location.href).searchParams.get('saved')==='1')root.querySelector('[data-classification-state]').textContent+=' · Classification saved and audited. Refresh Monday recommendations or calculate a new report to see the revised period.';
})();
