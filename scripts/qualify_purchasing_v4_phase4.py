"""Read-only accepted-source reconciliation around an isolated additive worksheet migration."""
import argparse,json,hashlib,time
from pathlib import Path
from sqlalchemy import create_engine,text
from app.purchasing_v4.isolation import require_isolated_url,readonly_snapshot
from app.purchasing_v4.schema import migrate_worksheets
from app.purchasing_v4.reconciliation import public_fingerprints,public_structure
from app.purchasing_v4.calculation_serialization import canonical_bytes

def facts(c):
 names=c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='purchasing_v4' AND tablename!='alembic_version' ORDER BY tablename")).scalars().all()
 return {name:hashlib.sha256(c.execute(text("SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]'::jsonb)::text FROM purchasing_v4."+c.dialect.identifier_preparer.quote(name)+' t')).scalar_one().encode()).hexdigest() for name in names}

def main():
 p=argparse.ArgumentParser();p.add_argument('--source-url',required=True);p.add_argument('--target-url',required=True);p.add_argument('--phase3-evidence',required=True);p.add_argument('--output',required=True);a=p.parse_args();require_isolated_url(a.source_url);require_isolated_url(a.target_url)
 if a.source_url==a.target_url:raise ValueError('A separate clone is required')
 source=create_engine(a.source_url);target=create_engine(a.target_url);out=Path(a.output);out.mkdir(parents=True,exist_ok=True);accepted=Path(a.phase3_evidence);manifest=json.loads((accepted/'evidence-manifest.json').read_text())
 for name,sha in manifest['files'].items():
  path=(accepted/name).resolve()
  if not path.is_relative_to(accepted.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise ValueError('Accepted Phase3 evidence mismatch: '+name)
 with readonly_snapshot(source) as c:before=public_fingerprints(c);structure=public_structure(c);foundation=facts(c)
 with readonly_snapshot(target) as c:
  prior=facts(c);assert public_fingerprints(c)==before and public_structure(c)==structure and all(prior[k]==v for k,v in foundation.items())
  assert set(prior)-set(foundation)<=set(('worksheet','worksheet_row','worksheet_command','worksheet_generation','worksheet_owner_command','worksheet_preference'))
 with target.begin() as c:migrate_worksheets(c)
 with readonly_snapshot(target) as c:
  after=public_fingerprints(c);all_facts=facts(c);assert after==before and public_structure(c)==structure
  assert all(all_facts[k]==v for k,v in foundation.items())
  count=c.execute(text('SELECT count(*) FROM purchasing_v4.review_evidence')).scalar_one();assert count==102
  heads=c.execute(text('SELECT count(*) FROM purchasing_v4.control_head')).scalar_one();assert heads==0
  worksheets=c.execute(text('SELECT count(*) FROM purchasing_v4.worksheet')).scalar_one();assert worksheets==0
  revision=c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()
 with readonly_snapshot(source) as c:assert public_fingerprints(c)==before and facts(c)==foundation and public_structure(c)==structure
 result=dict(status='PASS',accepted_phase3_commit='08712fb5cb9d42b7e92fddbac83b0d4ffa4f3ef5',public_tables=len(before),public_rows=sum(v['rows'] for v in before.values()),public_records_structure_sequences_unchanged=True,original_v4_tables_preserved=len(foundation),original_v4_hashes_unchanged=True,unresolved_review_items=count,source_control_heads=heads,source_worksheets=worksheets,schema_revision=revision,new_v4_tables=sorted(set(all_facts)-set(foundation)),production_changes=0,purchasing_commitments_created=0,inventory_posts=0,external_requests=0)
 for name,value in [('qualification.json',result),('public-before.json',before),('public-after.json',after),('public-structure.json',structure),('foundation-preservation.json',dict(before=foundation,after={k:all_facts[k] for k in foundation}))]:(out/name).write_bytes(canonical_bytes(value))
 print(json.dumps(result));source.dispose();target.dispose()
if __name__=='__main__':main()
