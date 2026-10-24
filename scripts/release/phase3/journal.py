"""One private, fsynced hash-chained journal and environment-scoped flock."""
import fcntl
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from scripts.release.phase3.manifest import digest


class Busy(RuntimeError): pass


def atomic(path, value):
    path=Path(path);temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,sort_keys=True,allow_nan=False);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


class Journal:
    def __init__(self, directory, manifest_id, lock_path=None):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.directory/'release.json';self.manifest_id=manifest_id;self.lock=None
        self.lock_path=Path(lock_path) if lock_path else self.directory/'environment.lock'

    def __enter__(self):
        self.lock=open(self.lock_path,'a',opener=lambda p,f:os.open(p,f,0o600))
        try: fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close();raise Busy('Another release owns this environment') from None
        try:
            self.data=json.loads(self.path.read_text()) if self.path.exists() else {'manifest_id':self.manifest_id,'events':[],'actions':{},'status':'NEW'}
            if self.data['manifest_id'] != self.manifest_id: raise ValueError('Journal belongs to another candidate')
            previous=None
            for e in self.data['events']:
                if e['previous'] != previous or e['hash'] != digest({k:v for k,v in e.items() if k != 'hash'}):
                    raise ValueError('Journal audit chain corrupted')
                previous=e['hash']
            # Snapshot is bound to event tail; edits to actions/status fail closed.
            if self.data.get('snapshot_sha256') != digest({k:v for k,v in self.data.items() if k not in ('events','snapshot_sha256')}) and self.data['events']:
                raise ValueError('Journal state corrupted')
            return self
        except BaseException:
            self.__exit__(None,None,None);raise

    def event(self, kind, **fields):
        e={'sequence':len(self.data['events'])+1,'at':datetime.now(timezone.utc).isoformat(),
           'kind':kind,'previous':self.data['events'][-1]['hash'] if self.data['events'] else None,**fields}
        e['hash']=digest(e);self.data['events'].append(e)
        self.data['snapshot_sha256']=digest({k:v for k,v in self.data.items() if k not in ('events','snapshot_sha256')})
        atomic(self.path,self.data)

    def __exit__(self,*_):
        if self.lock:
            fcntl.flock(self.lock,fcntl.LOCK_UN);self.lock.close();self.lock=None
