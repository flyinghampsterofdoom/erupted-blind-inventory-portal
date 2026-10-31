"""Use one existing PRODUCT authority; no store-specific purchasing switch."""
from sqlalchemy import text
import pytest
from test_purchasing_v4_worksheets import sheet,ready,engine,template,reading,pytestmark
from test_purchasing_v4_controls import execute,change
from app.purchasing_v4.purchasing_controls import list_products
from app.purchasing_v4 import controls,worksheets


def test_global_status_search_filter_sort_and_restore_preserve_manual_and_worksheets(sheet):
    e,state=sheet;vid=state['rows'][0]['variation_id'];sid=state['rows'][0]['supplier_id']
    with e.connect() as c:
        before={t:c.scalar(text('select count(*) from '+t)) for t in ('par_levels','purchase_orders','purchasing_v4.worksheet','purchasing_v4.worksheet_generation')}
        rows=list_products(c,limit=100)['rows'];r=next(x for x in rows if x['variation_id']==vid)
        assert r['supplier_id']==sid and not r['no_reorder']
        assert any(x['variation_id']==vid for x in list_products(c,supplier=sid,search=r['sku'],sort='product')['rows'])
        assert [x['product'].lower() for x in rows]==sorted(x['product'].lower() for x in rows)
    execute(e,[change('PRODUCT',vid,dict(variation_id=vid,no_reorder=True,coverage=None))])
    with e.connect() as c:
        assert any(x['variation_id']==vid for x in list_products(c,status='no_reorder')['rows'])
        assert not any(x['variation_id']==vid for x in list_products(c,status='active')['rows'])
        payload=controls.live(controls.heads(c,{('PRODUCT',vid)})[('PRODUCT',vid)])
        assert set(payload)=={'variation_id','no_reorder','coverage'} and 'store_id' not in payload
        assert worksheets.snapshot(c,state['session']['id'],1)['session']['version']==0
    execute(e,[change('PRODUCT',vid,dict(variation_id=vid,no_reorder=False,coverage=None),1)])
    with e.connect() as c:
        assert any(x['variation_id']==vid for x in list_products(c,status='active')['rows'])
        assert before=={t:c.scalar(text('select count(*) from '+t)) for t in before}


@pytest.mark.parametrize('kwargs',[dict(sort='product;delete'),dict(direction='invalid'),dict(status='store-only'),dict(limit=101),dict(offset=-1),dict(supplier='unknown')])
def test_bounded_controls_reject_invalid_filters(sheet,kwargs):
    e,_=sheet
    with e.connect() as c:
        with pytest.raises(controls.InvalidPolicy):list_products(c,**kwargs)
