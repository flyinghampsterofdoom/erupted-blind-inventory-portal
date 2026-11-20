"""Legacy cross-store receipts conserve incoming and allow exact review."""
from copy import deepcopy
import os
import pytest
from sqlalchemy import text
from app.purchasing_v4.evidence_rules import incoming_quantity
from app.purchasing_v4.receiving_supply import incoming_remainder
from test_purchasing_v4_submission import purchasing, sheet, ready, engine, template, reading, review


def rows(ordered, allocated, received, closed=None, direct=False):
    return [dict(id=1,purchase_order_id=129,variation_id='FIXTURE_V1',sku='fixture',
                 ordered_qty=ordered,received_qty_total=sum(received),store_id=sid,
                 allocated_qty=a,store_received_qty=r,closed=c,worksheet_direct=direct)
            for sid,a,r,c in zip((2,4,6,9),allocated,received,closed or (0,0,0,0))]


@pytest.mark.parametrize('direct',[False,True])
@pytest.mark.parametrize('ordered,allocated,received,expected',[
    (33,(4,10,19,0),(5,5,5,0),{2:0,4:5,6:13,9:0}),
    (4,(1,2,0,1),(0,3,0,0),{2:1,4:0,6:0,9:0}),
    (70,(15,0,15,40),(15,15,15,25),{2:0,4:0,6:0,9:0}),
    (55,(15,0,0,40),(15,15,10,15),{2:0,4:0,6:0,9:0}),
    (20,(5,5,5,5),(1,2,0,0),{2:4,4:3,6:5,9:5}),
])
def test_receipt_screen_does_not_change_actual_incoming(ordered,allocated,received,expected,direct):
    source=rows(ordered,allocated,received,direct=direct);before=deepcopy(source)
    result,reason,references=incoming_quantity(source,'FIXTURE_V1')
    assert not reason and result==expected
    assert references==('purchase_order_lines:1',)
    assert sum(result.values())==ordered-sum(received)
    assert incoming_remainder(source)==result and source==before


def test_legacy_closure_and_cross_store_receipt_conserve_open_balance():
    source=rows(33,(4,10,19,0),(5,5,5,0),(0,2,0,0))
    result,reason,_=incoming_quantity(source,'FIXTURE_V1')
    assert not reason and result=={2:0,4:3,6:13,9:0} and sum(result.values())==16


@pytest.mark.parametrize('field,value,reason',[
    ('ordered_qty',34,'CONFLICTING_INCOMING_ALLOCATION'),
    ('received_qty_total',16,'CONFLICTING_INCOMING_ALLOCATION'),
    ('allocated_qty',-1,'INCOMPLETE_INCOMING_BALANCES'),
    ('closed',19,'INVALID_CLOSURE_BALANCE'),
])
def test_invalid_source_facts_still_block(field,value,reason):
    source=rows(33,(4,10,19,0),(5,5,5,0))
    if field in ('ordered_qty','received_qty_total'):
        for row in source:row[field]=value
    else:source[0][field]=value
    values,why,_=incoming_quantity(source,'FIXTURE_V1')
    assert values is None and why==reason


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_ADMIN_URL'),reason='Explicit disposable PostgreSQL required')
def test_submission_review_accepts_legacy_partial_receipt_without_rewriting_facts(purchasing):
    e,state=purchasing
    with e.begin() as c:
        allocations=[dict(r) for r in c.execute(text('SELECT * FROM purchase_order_store_allocations WHERE purchase_order_line_id=1 ORDER BY store_id')).mappings()]
        ordered=c.scalar(text('SELECT ordered_qty FROM purchase_order_lines WHERE id=1'))
        target=next(a for a in allocations if a['allocated_qty']<ordered)
        received=target['allocated_qty']+1
        c.execute(text('UPDATE purchase_order_store_allocations SET store_received_qty=CASE WHEN id=:id THEN :qty ELSE 0 END WHERE purchase_order_line_id=1'),dict(id=target['id'],qty=received))
        c.execute(text('UPDATE purchase_order_lines SET received_qty_total=:qty WHERE id=1'),dict(qty=received))
    def facts():
        with e.connect() as c:
            return list(c.execute(text('SELECT row_to_json(l)::text FROM purchase_order_lines l ORDER BY id')).scalars()),list(c.execute(text('SELECT row_to_json(a)::text FROM purchase_order_store_allocations a ORDER BY id')).scalars())
    before=facts();result=review(purchasing)
    assert sum(result['lines'][0]['incoming'])==ordered-received
    assert facts()==before
