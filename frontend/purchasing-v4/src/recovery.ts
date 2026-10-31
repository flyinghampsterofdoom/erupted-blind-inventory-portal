import type {Action,Snapshot,View,Change,DefaultChange} from './model.ts';
export type Batch={id:string;expected_version:number;changes:Change[];defaults:DefaultChange[];opIds:string[]};
export type Saved={snapshot?:Snapshot;commands:Action[];batch?:Batch;ackVersion?:number;view?:View;needsChoice?:boolean};
export class Journal{
 db:IDBDatabase|null=null;key:string;failed=false;
 constructor(owner:number,session:string){this.key=owner+':'+session;}
 async open(){this.db=await new Promise<IDBDatabase>((resolve,reject)=>{const request=indexedDB.open('erupted-v4-worksheet',1);request.onupgradeneeded=()=>{for(const name of ['snapshot','command','meta'])request.result.createObjectStore(name);};request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});}
 async write(store:string,key:string,value:unknown){if(!this.db)throw Error('Local recovery is unavailable.');await new Promise<void>((resolve,reject)=>{const tx=this.db!.transaction(store,'readwrite');tx.objectStore(store).put(value,key);tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);});}
 async read(store:string,key:string){return await new Promise<unknown>((resolve,reject)=>{const req=this.db!.transaction(store).objectStore(store).get(key);req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});}
 async load():Promise<Saved>{
  if(!this.db)throw Error('Local recovery is unavailable.');
  const tx=this.db.transaction(['snapshot','command','meta'],'readonly');
  const request=<T>(req:IDBRequest<T>)=>new Promise<T>((resolve,reject)=>{req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});
  const [snapshot,meta,commands]=await Promise.all([request(tx.objectStore('snapshot').get(this.key)),request(tx.objectStore('meta').get(this.key)),request(tx.objectStore('command').getAll(IDBKeyRange.bound(this.key+':',this.key+':\uffff')))]);
  return {snapshot:snapshot as Snapshot|undefined,commands:(commands as Action[]).sort((a,b)=>a.seq-b.seq),...meta};
 }
 async checkpoint(snapshot:Snapshot,commands:Action[],meta:Omit<Saved,'commands'|'snapshot'>){
  if(!this.db)throw Error('Local recovery is unavailable.');
  await new Promise<void>((resolve,reject)=>{const tx=this.db!.transaction(['snapshot','command','meta'],'readwrite');tx.objectStore('snapshot').put(snapshot,this.key);for(const action of commands)tx.objectStore('command').put(action,this.key+':'+action.id);tx.objectStore('meta').put(meta,this.key);tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);});
 }
 async command(action:Action){await this.write('command',this.key+':'+action.id,action);}
 async snapshot(value:Snapshot){await this.write('snapshot',this.key,value);}
 async meta(value:Omit<Saved,'commands'|'snapshot'>){await this.write('meta',this.key,value);}
}
