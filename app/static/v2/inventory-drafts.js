/* Immutable, authorized identities and versioned keys. Quarantine never imports work. */
(() => {
  const encoder=new TextEncoder(),decoder=new TextDecoder();
  const prefix=s=>`inventory-draft:v2:${s.principal_uuid}:${s.store_uuid}:${s.kind}:${s.record_uuid}:`;
  const encode=b=>{let s='';for(let i=0;i<b.length;i+=8192)s+=String.fromCharCode(...b.subarray(i,i+8192));return btoa(s);};
  const decode=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
  const equal=(a,b)=>['principal_uuid','store_uuid','kind','record_uuid'].every(k=>a?.[k]===b?.[k]);
  const headers=s=>({'X-Inventory-Draft':'true','X-Inventory-Identity':s.record_uuid,'X-Inventory-Principal':s.principal_uuid,'X-Inventory-Store':s.store_uuid});
  async function fetchKeys(url){
    const r=await fetch(url,{credentials:'same-origin',cache:'no-store',headers:{'X-Requested-With':'autosave'}});
    if(!r.ok||r.redirected){const e=Error(r.status===503?'Protected draft keys are unavailable. Drafts are retained; ask an administrator for recovery assistance.':'Sign in with the same account and restore access to this store, then retry.');e.status=r.status;throw e;}
    return r.json();
  }
  async function decrypt(envelope,data){
    if(envelope.version!==2||!equal(envelope.scope,data.scope))throw Error('Draft identity differs; recovery is quarantined and retained.');
    if(!data.keys?.[envelope.key_id])throw Error('A historical draft key is unavailable. The protected draft is retained; ask an administrator to restore the retained key.');
    const key=await crypto.subtle.importKey('raw',decode(data.keys[envelope.key_id]),'AES-GCM',false,['decrypt']);
    const aad=encoder.encode(prefix(envelope.scope)+envelope.key_id);
    const plain=await crypto.subtle.decrypt({name:'AES-GCM',iv:decode(envelope.iv),additionalData:aad},key,decode(envelope.body));
    return JSON.parse(decoder.decode(plain));
  }
  const recoveryURL=s=>`/store/draft-recovery/${encodeURIComponent(s.kind)}/${encodeURIComponent(s.record_uuid)}/key?principal_uuid=${encodeURIComponent(s.principal_uuid)}&store_uuid=${encodeURIComponent(s.store_uuid)}&record_id=${encodeURIComponent(s.record_id)}`;
  function quarantine(items){
    if(!items.length||!document.createElement)return;
    let host=document.getElementById('inventory-quarantine');
    if(!host){host=document.createElement('section');host.id='inventory-quarantine';document.body.append(host);}
    host.replaceChildren();
    const message=document.createElement('p');message.setAttribute('role','status');message.textContent='Protected work needs recovery review. Its identity or key could not be matched to this count. It is retained on this device and will never be imported or submitted here.';host.append(message);
    const button=document.createElement('button');button.type='button';button.textContent='Review quarantined work with original store authorization';host.append(button);
    button.onclick=async()=>{
      button.disabled=true;
      try{
        for(const item of items){
          const e=JSON.parse(item.raw);if(e.version!==2)throw Error('This older draft has no verifiable permanent identity. Retain this browser data and contact an administrator; automatic import is prohibited.');
          const keys=await fetchKeys(recoveryURL(e.scope));const value=await decrypt(e,keys);
          if(value.unverifiedLegacyIdentity)throw Error('Legacy ownership requires administrator verification; no automatic recovery is permitted.');
          // Values are shown as text, only after fresh original account/store authorization.
          const detail=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');
          summary.textContent='Original draft '+e.scope.record_uuid+' · review only';
          pre.textContent=JSON.stringify({labels:value.labels||{},values:value.values,fields:value.fields},null,2);detail.append(summary,pre);host.append(detail);
        }
        message.textContent='Authorized recovery review only. These quantities remain quarantined. A Lead must verify the original count before any separate audited reconciliation.';
      }catch(_){message.textContent='Recovery cannot be authorized or a retained key is unavailable. The encrypted work has been retained. Restore original store access or ask an administrator for recovery assistance.';button.disabled=false;}
    };
    async function hide(){
      for(const detail of host.querySelectorAll('details'))detail.remove();button.disabled=false;
    }
    // Hide review on every focus transition and periodically. Reopening always reauthorizes.
    document.addEventListener('visibilitychange',()=>{void hide();});
    setInterval(()=>{void hide();},15000);
  }
  async function open(url,expected){
    const data=await fetchKeys(url);
    if(Object.keys(expected).some(k=>String(data.scope[k])!==String(expected[k])))throw Error('Stock-take or account identity changed. Protected drafts are retained for recovery review. Reopen the original count.');
    if(!crypto.subtle)throw Error('Protected draft storage requires a secure browser connection.');
    const start=prefix(data.scope),own=start+crypto.randomUUID();
    const active=data.active_key_id;
    if(!data.keys?.[active])throw Error('Protected draft keys are unavailable; work is retained.');
    const key=await crypto.subtle.importKey('raw',decode(data.keys[active]),'AES-GCM',false,['encrypt']);
    const aad=encoder.encode(start+active);
    // A storage check must succeed before entry is enabled.
    localStorage.setItem(own,'storage-check');localStorage.removeItem(own);
    const quarantined=[];
    for(const name of Object.keys(localStorage).filter(n=>n.startsWith('inventory-draft:'))){
      if(name.startsWith(start))continue;
      const raw=localStorage.getItem(name);
      try{const e=JSON.parse(raw);if(e.scope?.principal_uuid===data.scope.principal_uuid&&e.scope?.store_uuid===data.scope.store_uuid&&e.scope?.kind===data.scope.kind&&String(e.scope?.record_id)===String(data.scope.record_id))quarantined.push({name,raw});}
      catch(_){}
      if(name.startsWith(`inventory-draft:v1:${data.scope.principal_id}:${data.scope.store_id}:${data.scope.kind}:${data.scope.record_id}:`))quarantined.push({name,raw});
    }
    quarantine(quarantined);
    let chain=Promise.resolve(),adopted=null;
    async function records(){
      const result=[];
      for(const name of Object.keys(localStorage).filter(n=>n.startsWith(start))){
        const raw=localStorage.getItem(name);
        try{const e=JSON.parse(raw);result.push({name,raw,value:await decrypt(e,data)});}
        catch(_){quarantine([{name,raw},...quarantined]);throw Error('A protected recovery draft could not be opened. It has been quarantined and retained. Restore missing historical keys before continuing.');}
      }
      return result.sort((a,b)=>b.value.savedAt-a.value.savedAt);
    }
    function save(value){
      const snapshot=JSON.stringify({...value,labels:data.labels||{},savedAt:Date.now()});
      chain=chain.catch(()=>{}).then(async()=>{
        const iv=crypto.getRandomValues(new Uint8Array(12));
        const body=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:aad},key,encoder.encode(snapshot));
        localStorage.setItem(own,JSON.stringify({version:2,scope:data.scope,key_id:active,iv:encode(iv),body:encode(new Uint8Array(body))}));
        if(adopted&&localStorage.getItem(adopted.name)===adopted.raw)localStorage.removeItem(adopted.name);adopted=null;
      });return chain;
    }
    async function quarantineLegacy(legacyKey){
      const raw=sessionStorage.getItem(legacyKey);if(!raw)return;
      // Old buffers have no provable count identity. Encrypt separately; never adopt.
      const scope={...data.scope,record_uuid:crypto.randomUUID()};const keys=await fetchKeys(recoveryURL(scope));
      const kid=keys.active_key_id,k=await crypto.subtle.importKey('raw',decode(keys.keys[kid]),'AES-GCM',false,['encrypt']);
      const iv=crypto.getRandomValues(new Uint8Array(12)),value=JSON.parse(raw);
      const body=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:encoder.encode(prefix(scope)+kid)},k,encoder.encode(JSON.stringify({...value,labels:{},unverifiedLegacyIdentity:true})));
      const name=prefix(scope)+crypto.randomUUID(),encrypted=JSON.stringify({version:2,scope,key_id:kid,iv:encode(iv),body:encode(new Uint8Array(body))});
      localStorage.setItem(name,encrypted);if(sessionStorage.getItem(legacyKey)===raw)sessionStorage.removeItem(legacyKey);quarantine([{name,raw:encrypted},...quarantined]);
    }
    return {recordName:own,scope:data.scope,labels:data.labels||{},state:data.state,context:data.working_context,records,quarantineLegacy,
      headers:()=>headers(data.scope),adopt:r=>{adopted=r?.name?r:null;},save,flush:()=>chain,
      remove:r=>{if(localStorage.getItem(r.name)===r.raw)localStorage.removeItem(r.name);},
      clear:async()=>{await chain;localStorage.removeItem(own);if(adopted&&localStorage.getItem(adopted.name)===adopted.raw)localStorage.removeItem(adopted.name);adopted=null;}};
  }
  window.eruptedDrafts={open};
  const center=document.getElementById('inventory-recovery-center');
  if(center){
    try{
      const items=[];
      for(const name of Object.keys(localStorage).filter(n=>n.startsWith('inventory-draft:v2:'))){
        const raw=localStorage.getItem(name);
        try{const e=JSON.parse(raw);if(e.scope?.principal_uuid===center.dataset.principalUuid&&e.scope?.store_uuid===center.dataset.storeUuid)items.push({name,raw});}catch(_){}
      }
      if(items.length)quarantine(items);else center.textContent='No protected drafts found for your current account and authorized store on this browser.';
    }catch(_){center.textContent='Device storage could not be read. Keep this browser data and ask for recovery assistance.';}
  }
})();
