/* Encrypted, account/store/record-scoped recovery. Keys are released only by authorized endpoints. */
(() => {
  const encoder = new TextEncoder(), decoder = new TextDecoder();
  const prefix = scope => `inventory-draft:v1:${scope.principal_id}:${scope.store_id}:${scope.kind}:${scope.record_id}:`;
  const encode = bytes => {let text='';for(let i=0;i<bytes.length;i+=8192) text+=String.fromCharCode(...bytes.subarray(i,i+8192));return btoa(text);};
  const decode = text => Uint8Array.from(atob(text), char => char.charCodeAt(0));
  async function open(url, expected) {
    const response = await fetch(url, {credentials:'same-origin', cache:'no-store', headers:{'X-Requested-With':'autosave'}});
    if (!response.ok || response.redirected) {
      const error = new Error(response.status === 503 ? 'Protected draft storage is unavailable. Ask an administrator for assistance.' : 'Sign in with the same account and restore access to this store, then retry.');
      error.status = response.status; throw error;
    }
    const data = await response.json();
    if (Object.keys(expected).some(key => String(data.scope[key]) !== String(expected[key]))) throw Error('Sign in with the same account before restoring this draft.');
    if (!crypto.subtle) throw Error('Protected draft storage requires a secure browser connection.');
    const key = await crypto.subtle.importKey('raw', decode(data.key), 'AES-GCM', false, ['encrypt','decrypt']);
    delete data.key;
    const start = prefix(data.scope), own = start + crypto.randomUUID(), aad = encoder.encode(start);
    // Test availability before allowing entry. Never fall back to plaintext storage.
    localStorage.setItem(own, 'storage-check'); localStorage.removeItem(own);
    let chain = Promise.resolve(), adopted = null;
    async function records() {
      const results = [];
      for (const name of Object.keys(localStorage).filter(name => name.startsWith(start))) {
        const raw = localStorage.getItem(name);
        try {
          const envelope = JSON.parse(raw);
          const plaintext = await crypto.subtle.decrypt({name:'AES-GCM',iv:decode(envelope.iv),additionalData:aad}, key, decode(envelope.body));
          results.push({name, raw, value:JSON.parse(decoder.decode(plaintext))});
        } catch (_) { throw Error('A protected recovery draft could not be opened. It has been retained. Ask for assistance before continuing.'); }
      }
      return results.sort((a,b) => b.value.savedAt - a.value.savedAt);
    }
    function save(value) {
      // Snapshot NOW; serialize encryption/writes so old async encryption cannot replace newer edits.
      const snapshot = JSON.stringify({...value, savedAt:Date.now()});
      chain = chain.catch(() => {}).then(async () => {
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const body = await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:aad}, key, encoder.encode(snapshot));
        localStorage.setItem(own, JSON.stringify({iv:encode(iv),body:encode(new Uint8Array(body))}));
        // Claim a restored snapshot only after its replacement is durable. Never delete another tab's newer write.
        if (adopted && localStorage.getItem(adopted.name) === adopted.raw) localStorage.removeItem(adopted.name);
        adopted = null;
      });
      return chain;
    }
    return {recordName:own,labels:data.labels||{},state:data.state, context:data.working_context, records, adopt:record=>{adopted=record?.name?record:null;}, save,
      flush:()=>chain, remove:record=>{if(localStorage.getItem(record.name)===record.raw)localStorage.removeItem(record.name);},
      clear:async()=>{await chain;localStorage.removeItem(own);if(adopted && localStorage.getItem(adopted.name)===adopted.raw)localStorage.removeItem(adopted.name);adopted=null;}, scope:data.scope};
  }
  window.eruptedDrafts = {open};
})();
