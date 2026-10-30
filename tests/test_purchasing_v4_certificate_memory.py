"""Certificate cardinality, immutable membership and fail-closed regression cases."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import pytest
from app.purchasing_v4 import evidence_rules as rules

START=datetime(2026,10,29,22,55,tzinfo=timezone.utc)
END=START+timedelta(seconds=10,microseconds=35960)

def fixture(n=5280):
    pairs=[[f'v{i//4}',i%4+1] for i in range(n)]
    proof=dict(id=12,source='INVENTORY',status='PARTIAL',started_at=START.isoformat(),
        completed_at=END.isoformat(),scope=dict(variation_ids=sorted({p[0] for p in pairs}),store_ids=[1,2,3,4]),
        evidence=dict(error_code='PARTIAL_COVERAGE',observed_pairs=pairs))
    row=dict(refresh_run_id=12,result='PARTIAL',error_code='PARTIAL_COVERAGE',started_at=START,completed_at=END,
        expected_pair_count=n+8,covered_pair_count=n,missing_pair_count=8)
    return row,proof


def load(row,proofs,n=1):
    calls=[]
    def fetch(sql,**params):
        calls.append((sql,params))
        if sql.startswith('SELECT i.*'):return [dict(row,square_variation_id=f'v{i//4}',store_id=i%4+1) for i in range(n)]
        return [dict(refresh_run_id=12,certificate=p) for p in proofs]
    return rules.load_inventory(fetch),calls


def test_production_sized_certificate_fetched_and_validated_once(monkeypatch):
    row,proof=fixture();compiled=[];original=rules._compile_partial_certificate
    def counted(value):compiled.append(1);return original(value)
    monkeypatch.setattr(rules,'_compile_partial_certificate',counted)
    rows,calls=load(row,[proof],5280)
    assert len(calls)==2 and calls[1][1]=={'refresh_ids':[12]} and len(compiled)==1
    assert len({id(r['_partial_certificate']) for r in rows})==1
    assert all(r['certificates']==[{'id':12}] for r in rows)
    assert all(rules.verified_partial_pair(r,r['square_variation_id'],r['store_id']) for r in rows)
    assert len(compiled)==1
    assert not rules.verified_partial_pair(rows[0],'missing',1)
    assert not rules.verified_partial_pair(rows[0],'v0',99)
    # Source payload mutations cannot alter the frozen validation result.
    proof['evidence']['observed_pairs'].clear()
    assert rules.verified_partial_pair(rows[0],'v0',1)
    changed=dict(rows[0],completed_at=END+timedelta(microseconds=1))
    assert not rules.verified_partial_pair(changed,'v0',1)


@pytest.mark.parametrize('case',['absent','duplicate','timestamp','source','status','error','counts','scope','store_type','pair_type','quantity_scope'])
def test_invalid_certificates_and_unknown_pairs_remain_excluded(case):
    row,proof=fixture(4);proofs=[proof]
    if case=='absent':proofs=[]
    if case=='duplicate':proofs.append(deepcopy(proof))
    if case=='timestamp':proof['completed_at']=(END+timedelta(microseconds=1)).isoformat()
    if case=='source':proof['source']='SALES'
    if case=='status':proof['status']='COMPLETE'
    if case=='error':proof['evidence']['error_code']='OTHER'
    if case=='counts':row['missing_pair_count']=9
    if case=='scope':proof['scope']['variation_ids']=[]
    if case=='store_type':proof['scope']['store_ids']=['1']
    if case=='pair_type':proof['evidence']['observed_pairs']=[['v0',True]]
    if case=='quantity_scope':proof['evidence']['observed_pairs']=[['v1',1]]
    rows,_=load(row,proofs)
    assert not rules.verified_partial_pair(rows[0],'v0',1)


def test_complete_runs_need_no_certificate_retrieval():
    row,_=fixture(4);row.update(result='COMPLETE',error_code=None)
    rows,calls=load(row,[])
    assert len(calls)==1 and not rules.verified_partial_pair(rows[0],'v0',1)


def test_row_freshness_and_quantities_still_checked_after_shared_validation():
    row,proof=fixture(4);rows,_=load(row,[proof]);r=rows[0]
    r.update(square_location_id='loc',refreshed_at=END,source_calculated_at=END,
             freshness_state='FRESH',counted_quantity=Decimal(7))
    args=dict(location='loc',variation_id='v0',store_id=1,as_of=END,max_age=timedelta(hours=36))
    assert rules.inventory_observation(r,**args).value==7
    r['freshness_state']='STALE'
    assert rules.inventory_observation(r,**args).value is None
    r['freshness_state']='FRESH'
    assert rules.inventory_observation(r,**dict(args,as_of=END+timedelta(hours=37))).value is None


@pytest.mark.parametrize('rows',[(),({'unicode':'é漢字','fraction':__import__('fractions').Fraction(2,3),'quantity':Decimal('1.2300')},),({'value':[None,True,False,4,{'z':'last','a':'first'}]},)*20])
def test_bounded_source_hash_is_exactly_the_original_canonical_digest(rows):
    import hashlib
    from app.purchasing_v4.calculation_serialization import canonical_bytes,evidence_digest
    exceptions=('UNKNOWN:é','preserved source')
    expected=hashlib.sha256(canonical_bytes(dict(rows=rows,as_of=END,source_exceptions=exceptions))).hexdigest()
    assert evidence_digest(rows,as_of=END,source_exceptions=exceptions)==expected


def test_source_hash_never_materializes_all_rows_again(monkeypatch):
    from app.purchasing_v4 import calculation_serialization as serialization
    original=serialization.canonical_bytes;rows=tuple({'row':i} for i in range(1000));seen=[]
    def bounded(value):
        assert value is not rows
        assert not isinstance(value,dict) or 'rows' not in value
        seen.append(value);return original(value)
    monkeypatch.setattr(serialization,'canonical_bytes',bounded)
    serialization.evidence_digest(rows,as_of=END)
    assert len(seen)==1002
