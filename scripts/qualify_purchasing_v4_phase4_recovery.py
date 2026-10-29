"""Cold synthetic restore, independent reconciliation and post-restore worksheet commands."""
import argparse,json,hashlib,subprocess,time,statistics
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine,text,event
from app.purchasing_v4.isolation import require_isolated_url,readonly_snapshot
from app.purchasing_v4.reconciliation import public_fingerprints,public_structure
from app.purchasing_v4 import worksheets


def fingerprints(engine):
 with readonly_snapshot(engine) as c:
  names=c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='purchasing_v4' ORDER BY tablename")).scalars().all()
  v4={name:hashlib.sha256(c.execute(text("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM purchasing_v4."+c.dialect.identifier_preparer.quote(name)+' t')).scalar_one().encode()).hexdigest() for name in names}
  return dict(public=public_fingerprints(c),public_structure=public_structure(c),v4=v4)


def constraints(engine):
 with engine.connect() as c:return {(r[0],r[1],r[2]):r[3] for r in c.execute(text("SELECT n.nspname,c.relname,con.conname,pg_get_constraintdef(con.oid) FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('public','purchasing_v4') ORDER BY n.nspname,c.relname,con.conname"))}


def main():
 p=argparse.ArgumentParser();p.add_argument('--source-url',required=True);p.add_argument('--target-url',required=True);p.add_argument('--pg-bin',required=True);p.add_argument('--output',required=True);p.add_argument('--reset-recovery',action='store_true');a=p.parse_args();source_url=require_isolated_url(a.source_url);target_url=require_isolated_url(a.target_url)
 if source_url.database!='erupted_v4_phase1_phase4_preview' or not target_url.database.startswith('erupted_v4_phase1_phase4_recovery') or not target_url.database.replace('_','').isalnum():raise ValueError('Named synthetic Phase4 preview and recovery databases only')
 if source_url.host!=target_url.host or source_url.port!=target_url.port:raise ValueError('Same owned isolated cluster required')
 source=create_engine(source_url);out=Path(a.output);out.mkdir(parents=True,exist_ok=True);pg=Path(a.pg_bin)
 with source.connect() as c:assert c.execute(text("SELECT count(*) FROM purchasing_v4.worksheet_generation WHERE evidence->>'fixture' IS DISTINCT FROM 'true'")).scalar_one()==0
 before=fingerprints(source);old_constraints=constraints(source);archive=out/'synthetic-recovery.dump'
 common=['-h','127.0.0.1','-p',str(source_url.port),'-U',source_url.username or 'postgres']
 subprocess.run([str(pg/'pg_dump'),*common,'-Fc','-f',str(archive),source_url.database],check=True,capture_output=True)
 if a.reset_recovery:subprocess.run([str(pg/'dropdb'),*common,'--if-exists',target_url.database],check=True,capture_output=True)
 subprocess.run([str(pg/'createdb'),*common,target_url.database],check=True,capture_output=True)
 subprocess.run([str(pg/'pg_restore'),*common,'--exit-on-error','-d',target_url.database,str(archive)],check=True,capture_output=True)
 target=create_engine(target_url);after=fingerprints(target)
 assert before['public']==after['public'] and before['v4']==after['v4']
 assert {k:v for k,v in before['public_structure'].items() if k!='constraints_digest'}=={k:v for k,v in after['public_structure'].items() if k!='constraints_digest'}
 new_constraints=constraints(target);assert old_constraints.keys()==new_constraints.keys();normalized=0
 with target.begin() as c:
  for (schema,table,name),definition in old_constraints.items():
   if definition==new_constraints[(schema,table,name)]:continue
   assert definition.startswith('CHECK ')
   quote=c.dialect.identifier_preparer.quote;c.execute(text('CREATE TEMP TABLE recovery_constraint_probe (LIKE '+quote(schema)+'.'+quote(table)+') ON COMMIT DROP'))
   c.execute(text('ALTER TABLE recovery_constraint_probe ADD CONSTRAINT probe '+definition))
   parsed=c.execute(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='recovery_constraint_probe'::regclass AND conname='probe'")).scalar_one()
   assert parsed==new_constraints[(schema,table,name)],(schema,table,name)
   c.execute(text('DROP TABLE recovery_constraint_probe'));normalized+=1
 with target.connect() as c:
  session=c.execute(text("SELECT id::text,owner_id FROM purchasing_v4.worksheet WHERE state='EDITABLE' ORDER BY updated_at DESC LIMIT 1")).mappings().one()
  original=worksheets.snapshot(c,session['id'],session['owner_id'])
 queries=[]
 def track(conn,cursor,statement,parameters,context,executemany):queries.append(statement)
 event.listen(target,'before_cursor_execute',track);samples=[];query_counts=[]
 for _ in range(20):
  with target.connect() as c:
   count=len(queries);started=time.perf_counter();worksheets.snapshot(c,session['id'],session['owner_id']);samples.append((time.perf_counter()-started)*1000);query_counts.append(len(queries)-count)
 changed=[]
 for row in [r for r in original['rows'] if not r['removed']][:100]:changed.append(dict(id=row['id'],value={key:([7]*4 if key=='packs' else [True]*4 if key=='touched' else row[key]) for key in worksheets.FIELDS}))
 command=str(uuid4());started=time.perf_counter();count=len(queries)
 with target.begin() as c:receipt=worksheets.patch(c,actor_id=session['owner_id'],session_id=session['id'],command_id=command,expected_version=original['session']['version'],changes=changed)
 bulk_ms=(time.perf_counter()-started)*1000;bulk_queries=len(queries)-count
 with target.begin() as c:assert worksheets.patch(c,actor_id=session['owner_id'],session_id=session['id'],command_id=command,expected_version=original['session']['version'],changes=changed)['replayed']
 with target.begin() as c:worksheets.patch(c,actor_id=session['owner_id'],session_id=session['id'],command_id=str(uuid4()),expected_version=receipt['version'],changes=receipt['inverse'])
 with target.connect() as c:
  restored=worksheets.snapshot(c,session['id'],session['owner_id']);assert restored['rows']==original['rows']
  assert restored['session']['stores']==original['session']['stores']
 rejected=[]
 for sql in ["UPDATE purchasing_v4.worksheet_row SET original='{}'::jsonb", "UPDATE purchasing_v4.worksheet SET stores='[]'::jsonb",'DELETE FROM purchasing_v4.worksheet_command']:
  try:
   with target.begin() as c:c.execute(text(sql))
  except Exception:rejected.append(sql)
 assert len(rejected)==3
 with readonly_snapshot(target) as c:assert public_fingerprints(c)==before['public'] and public_structure(c)==after['public_structure']
 assert fingerprints(source)==before
 result=dict(status='PASS',all_public_record_hashes_equal=True,public_sequences_columns_indexes_triggers_enums_equal=True,all_v4_table_hashes_equal=True,v4_tables=len(before['v4']),all_constraint_names_equal=True,normalized_check_definitions=normalized,constraint_verification='Original CHECK expressions independently reparsed under PG18; normalized definitions equal restored constraints',postrestore_replay_and_inverse_pass=True,pinned_store_bindings_preserved=True,immutable_original_and_audit_guards_pass=True,source_preview_unchanged=True,production_changes=0,archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
 sorted_samples=sorted(samples);perf=dict(snapshot_samples_ms=samples,snapshot_median_ms=statistics.median(samples),snapshot_p95_ms=sorted_samples[18],snapshot_p99_ms=sorted_samples[-1],snapshot_max_ms=sorted_samples[-1],snapshot_query_counts=query_counts,bulk100_ms=bulk_ms,bulk100_sql_queries=bulk_queries,retained_rows=len(original['rows']),active_rows=sum(not r['removed'] for r in original['rows']),stores=len(original['session']['stores']))
 (out/'recovery.json').write_text(json.dumps(result,indent=2));(out/'recovery-fingerprints.json').write_text(json.dumps(before,indent=2,default=str));(out/'backend-performance.json').write_text(json.dumps(perf,indent=2));print(json.dumps(result));source.dispose();target.dispose()
if __name__=='__main__':main()
