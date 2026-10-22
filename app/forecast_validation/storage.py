"""Dedicated append-only evidence database. Never opens the business write pool."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,os
from sqlalchemy import (create_engine,MetaData,Table,Column,String,Text,Integer,UniqueConstraint,select,text)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from .contracts import canonical,digest,Conflict,EvidenceError,stamp

metadata=MetaData()
runs=Table('fv_runs',metadata,Column('id',String,primary_key=True),Column('scope',String,nullable=False),Column('origin',String,nullable=False),Column('recorded_at',String,nullable=False),Column('expires_at',String,nullable=False),Column('kind',String,nullable=False),Column('payload',Text,nullable=False),Column('sha256',String,nullable=False),UniqueConstraint('scope','origin'))
evaluations=Table('fv_evaluations',metadata,Column('id',String,primary_key=True),Column('run_id',String,nullable=False),Column('horizon',Integer,nullable=False),Column('revision',String,nullable=False),Column('summary',Text,nullable=False),Column('recorded_at',String,nullable=False),Column('payload',Text,nullable=False),Column('sha256',String,nullable=False),UniqueConstraint('run_id','horizon','revision'))
attempts=Table('fv_attempts',metadata,Column('id',String,primary_key=True),Column('recorded_at',String,nullable=False),Column('operation',String,nullable=False),Column('status',String,nullable=False),Column('detail',Text,nullable=False))

class Store:
    def __init__(self,url,*,business_url=None,production=False):
        if not url:raise EvidenceError('Forecast evidence storage is not configured')
        parsed=make_url(url)
        if business_url:
            other=make_url(business_url)
            if (parsed.get_backend_name()=='postgresql' and other.get_backend_name()=='postgresql' and parsed.database==other.database) or (parsed.host,parsed.port or 5432,parsed.database)==(other.host,other.port or 5432,other.database):raise EvidenceError('Evidence storage must be a different database from purchasing')
        if production and parsed.get_backend_name()!='postgresql':raise EvidenceError('Production requires dedicated durable PostgreSQL storage')
        if parsed.get_backend_name() not in ('sqlite','postgresql'):raise EvidenceError('Unsupported evidence backend')
        self.url=parsed;self.engine=create_engine(url,connect_args={'options':'-c statement_timeout=10000 -c lock_timeout=2000'} if parsed.get_backend_name()=='postgresql' else {'timeout':5})
    def initialize(self):
        """Explicit provisioning command only; never called by page GET or forecast execution."""
        if self.url.get_backend_name()=='sqlite' and self.url.database not in (None,':memory:'):
            path=Path(self.url.database);path.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd);path.chmod(0o600)
        metadata.create_all(self.engine)
        with self.engine.begin() as c:
            if self.url.get_backend_name()=='postgresql':
                c.execute(text("CREATE OR REPLACE FUNCTION fv_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Forecast evidence is append-only'; END $$"))
                for t in metadata.tables:
                    c.execute(text(f'DROP TRIGGER IF EXISTS fv_immutable ON {t}'))
                    c.execute(text(f'CREATE TRIGGER fv_immutable BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION fv_reject_mutation()'))
                    c.execute(text(f'DROP TRIGGER IF EXISTS fv_no_truncate ON {t}'))
                    c.execute(text(f'CREATE TRIGGER fv_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION fv_reject_mutation()'))
            else:
                for t in metadata.tables:
                    for verb in ('UPDATE','DELETE'):
                        c.execute(text(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{verb.lower()} BEFORE {verb} ON {t} BEGIN SELECT RAISE(ABORT,'Forecast evidence is append-only'); END"))
    def _insert(self,table,values,keys):
        try:
            with self.engine.begin() as c:c.execute(table.insert().values(**values))
            return values['id']
        except IntegrityError:
            with self.engine.connect() as c:
                old=c.execute(select(table).where(*(table.c[k]==values[k] for k in keys))).mappings().first()
            if old and old['sha256']==values['sha256']:return old['id']
            raise Conflict('Existing immutable record has different evidence; create a new origin/revision')
    def add_run(self,payload,*,now=None):
        now=now or datetime.now(timezone.utc);snap=payload['snapshot'];origin=stamp(snap['origin']).isoformat();ident=digest({'scope':snap['scope'],'origin':origin})
        return self._insert(runs,{'id':ident,'scope':snap['scope'],'origin':origin,'kind':snap.get('kind','PROSPECTIVE'),'recorded_at':now.isoformat(),'expires_at':(now+timedelta(days=540)).isoformat(),'payload':canonical(payload),'sha256':digest(payload)},('scope','origin'))
    def add_evaluation(self,run_id,horizon,revision,payload):
        if not self.get_run(run_id):raise EvidenceError('Forecast not found')
        ident=digest({'run':run_id,'horizon':horizon,'revision':revision})
        return self._insert(evaluations,{'id':ident,'run_id':run_id,'horizon':horizon,'revision':revision,'summary':canonical({'metrics':[m for m in payload['metrics'] if m['dimension']=='class' and m['value']==m['cohort']],'classification':payload['classification'],'prospective':payload['prospective']}),'recorded_at':datetime.now(timezone.utc).isoformat(),'payload':canonical(payload),'sha256':digest(payload)},('run_id','horizon','revision'))
    def attempt(self,operation,status,detail):
        from uuid import uuid4
        with self.engine.begin() as c:c.execute(attempts.insert().values(id=uuid4().hex,recorded_at=datetime.now(timezone.utc).isoformat(),operation=operation,status=status,detail=detail[:1000]))
    def get_run(self,ident):
        with self.engine.connect() as c:r=c.execute(select(runs).where(runs.c.id==ident)).mappings().first()
        return self._decode(r)
    def _decode(self,row):
        if row is None:return None
        result=dict(row);payload=json.loads(result['payload'])
        if digest(payload)!=result['sha256']:raise EvidenceError('Stored evidence integrity check failed')
        result['payload']=payload;return result
    def list_runs(self,limit=40):
        with self.engine.connect() as c:rows=c.execute(select(runs.c.id,runs.c.origin,runs.c.recorded_at,runs.c.kind,runs.c.expires_at).order_by(runs.c.origin.desc()).limit(min(limit,100))).mappings().all()
        return [dict(r) for r in rows]
    def list_evaluations(self,run_id):
        with self.engine.connect() as c:rows=c.execute(select(evaluations).where(evaluations.c.run_id==run_id).order_by(evaluations.c.recorded_at,evaluations.c.id).limit(100)).mappings().all()
        return [self._decode(r) for r in rows]
    def failures(self):
        with self.engine.connect() as c:rows=c.execute(select(attempts).where(attempts.c.status=='FAILED').order_by(attempts.c.recorded_at.desc()).limit(10)).mappings().all()
        return [dict(r) for r in rows]

    def trends(self):
        with self.engine.connect() as c:rows=c.execute(select(runs.c.origin,runs.c.kind,evaluations.c.run_id,evaluations.c.horizon,evaluations.c.revision,evaluations.c.summary).join(evaluations,evaluations.c.run_id==runs.c.id).order_by(runs.c.origin.desc(),evaluations.c.recorded_at.desc()).limit(80)).mappings().all()
        return [{**dict(r),'summary':json.loads(r['summary'])} for r in rows]
