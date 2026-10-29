"""Explicit disposable SYNTHETIC desktop fixture; never reads production or creates commitments."""
import argparse,json,time
from pathlib import Path
from uuid import uuid4,uuid5,NAMESPACE_URL
from dataclasses import replace
from collections import defaultdict
from sqlalchemy import create_engine,text
from test_purchasing_v4_postgres import template,reading
from test_purchasing_v4_calculation import row,configuration,ASOF,known,history
from test_purchasing_v4_controls import profile,offer,change
from app.purchasing_v4.schema import migrate,migrate_controls,migrate_worksheets
from app.purchasing_v4.evidence import ReadOnlyEvidence
from app.purchasing_v4.storage import apply_plan
from app.purchasing_v4.management_read import catalog_projection
from app.purchasing_v4.controls import apply,create_supplier
from app.purchasing_v4 import worksheets
from app.purchasing_v4.calculation import recommend_batch,ManualRule
from app.security.passwords import hash_password

def main():
 p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--catalog',type=int,default=20000);p.add_argument('--rows',type=int,default=2000);p.add_argument('--vendors',type=int,default=30);p.add_argument('--database',default='erupted_v4_phase1_phase4_preview');p.add_argument('--source-namespace',default='synthetic-phase4-grid');a=p.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
 if not 1<=a.rows<=5000 or not a.rows<=a.catalog<=50000 or not 2<=a.vendors<=60:raise ValueError('Qualified synthetic fixture bounds')
 database=a.database;ns=a.source_namespace;
 if not database.startswith('erupted_v4_phase1_phase4_') or not database.replace('_','').isalnum() or not ns.startswith('synthetic-'):raise ValueError('Explicit synthetic Phase4 fixture identity required')
 gen=template.__wrapped__();admin,name=next(gen)
 with admin.connect() as c:c.execute(text('CREATE DATABASE '+database+' TEMPLATE '+name))
 try:next(gen)
 except StopIteration:pass
 engine=create_engine('postgresql+psycopg://postgres@127.0.0.1:58365/'+database)
 names=['Geek Bar Pulse','Lost Mary MO20000 Pro','RAZ DC25000','Juice Head Freebase','ZYN Nicotine Pouches','Coastal Reserve Glass','Boulder Rock Replacement Pods','Orion Bar 7500','King Palm Cones','Cloud Haven Salt Nic']
 flavors=['Blue Razz Ice','Watermelon Mango','Strawberry Kiwi','Mint · Cool Spearmint','Peach Berry','Tropical Passion Fruit','Grape Ice','Cherry Lemon','Original Tobacco','Rainbow Candy']
 with engine.begin() as c:
  c.execute(text('UPDATE principals SET password_hash=:hash WHERE id=1'),{'hash':hash_password('FixturePhase4!2026')})
  c.execute(text("UPDATE stores SET name=CASE id WHEN 1 THEN 'Andresen' ELSE 'HWY 99' END"))
  c.execute(text("INSERT INTO stores(id,name,square_location_id,active) VALUES (3,'SR 503','FIXTURE_LOC3',true),(4,'Longview','FIXTURE_LOC4',true)"))
  c.execute(text("""INSERT INTO ordering_catalog_identity(square_variation_id,square_item_id,sku,item_name,variation_name,catalog_attributes,last_seen_at)
  SELECT 'GRID_V'||n,'GRID_I'||n,'EV'||lpad(n::text,7,'0'),(CAST(:names AS text[]))[1+((n-1)%10)],(CAST(:flavors AS text[]))[1+((n-1)%10)]||' / '||lpad(n::text,5,'0'),jsonb_build_object('category_ids',jsonb_build_array('FIXTURE_VAPOR'),'gtin','000'||lpad(n::text,10,'0')),'2026-10-27T10:00Z'::timestamptz FROM generate_series(1,:count) n"""),dict(names=names,flavors=flavors,count=a.catalog))
 with reading(engine) as c:plan=ReadOnlyEvidence(c).identity_plan(ns)
 with engine.begin() as c:migrate(c);apply_plan(c,plan);migrate_controls(c);catalog_projection(c,source_namespace=ns);migrate_worksheets(c)
 suppliers=[]
 labels=['Atlas Trade Supply','Zephyr Imports','Big Wholesale','Streamline Distribution','Pacific Vapor Supply','Juice Head Wholesale','Northwest Smoke Supply','Cascade Merchants','Harbor Trading Co.','Evergreen Supply']
 for i in range(a.vendors):
  sid=str(uuid5(NAMESPACE_URL,ns+'/supplier/'+str(i)));name=labels[i] if i<len(labels) else f'Fixture Wholesale {i+1:02}'
  with engine.begin() as c:create_supplier(c,actor_id=1,command_id=str(uuid4()),reason='SYNTHETIC fixture owner supplier approval',supplier_id=sid,profile=dict(profile(name),confirmation_enabled=i%5==0))
  suppliers.append(sid)
 with engine.connect() as c:ids=c.execute(text("SELECT variation_id::text FROM purchasing_v4.management_catalog WHERE square_variation_id LIKE 'GRID_V%' ORDER BY square_variation_id LIMIT :limit"),{'limit':a.rows}).scalars().all()
 changes=[]
 for i,vid in enumerate(ids):
  default=suppliers[i%a.vendors]
  for sid in dict.fromkeys([default,*suppliers[:2]]):
   changes.append(change('OFFER',vid+':'+sid,dict(offer(vid,sid,pack=[5,10,6,12][i%4],cost=None if i%47==0 else ['1.125','2.40','3.675','0.85'][i%4]),vendor_sku='V'+str(i).zfill(7))))
  changes.append(change('ASSIGNMENT',vid,dict(variation_id=vid,supplier_id=default)))
 for start in range(0,len(changes),100):
  with engine.begin() as c:apply(c,actor_id=1,command_id=str(uuid4()),reason='SYNTHETIC fixture owner approved offers/defaults',changes=changes[start:start+100])
 with engine.connect() as c:rows=worksheets.catalog_rows(c,ids,ns)
 # Accepted engine supplies all pinned quantities. Histories here are explicit synthetic mathematics, never reconstructed business facts.
 config=configuration(negative_inventory='abstain_on_negative');inputs=[];histories={}
 for i,r in enumerate(rows):
  pattern=(5+i%20,8+i%30)
  if pattern not in histories:histories[pattern]=history(old_units=pattern[0],recent_units=pattern[1])
  for store in range(4):inputs.append(row(variation_id=r['variation_id'],store_id=str(store+1),pack_size=r['pack_size'],on_hand=known(20 if i%19==0 else i%13),manual=ManualRule('synthetic-manual',5,20) if i%7==0 else None,history=() if i%61==0 else histories[pattern],no_reorder=i%113==0))
 inputs_by_key={(r.variation_id,r.store_id):r for r in inputs}
 batch=recommend_batch(tuple(inputs),config,as_of=ASOF,input_digest='synthetic-phase4-evidence-seed-20261027');by={r['variation_id']:r for r in rows}
 for result in batch.recommendations:
  r=by[result.variation_id];index=int(result.store_id)-1;r['packs'][index]=result.suggested_packs if result.raw_need_units is not None and result.pack_size is not None else None;r['advice_units'][index]=str(result.raw_need_units) if result.raw_need_units is not None else None;r['policy'][index]=worksheets.policy_label(inputs_by_key[(result.variation_id,result.store_id)]);r['warnings']=sorted(set(r['warnings'])|set(result.reasons))
 with engine.begin() as c:state=worksheets.create(c,actor_id=1,command_id=str(uuid5(NAMESPACE_URL,ns+'/worksheet')),title='Weekly purchasing · desktop qualification',rows=rows,generation=dict(configuration_digest=config.content_digest,input_digest='synthetic-phase4-evidence-seed-20261027',as_of=ASOF.isoformat(),fixture=True,engine='accepted-phase2',recommendation_count=len(batch.recommendations)))
 metadata=dict(database=database,source_namespace=ns,rows=a.rows,catalog=a.catalog,vendors=a.vendors,session_id=state['session']['id'],username='v4-fixture',synthetic_password='FixturePhase4!2026',fixture_only=True)
 (out/'preview-fixture.json').write_text(json.dumps(metadata,indent=2));print(json.dumps(metadata))
if __name__=='__main__':main()
