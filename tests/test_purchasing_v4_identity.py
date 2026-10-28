import copy
import pytest
from app.purchasing_v4.identity import build_plan, identity


def source():
    return dict(source_namespace='fixture-square-source',
        catalog=[dict(square_variation_id='V1', square_item_id='I1', sku='SKU', item_name='A'),
                 dict(square_variation_id='V2', square_item_id='I1', sku='SKU', item_name='A')],
        vendors=[dict(id=1,square_vendor_id='S1',active=True),dict(id=2,square_vendor_id='S2',active=True)],
        configs=[dict(id=1,vendor_id=1,square_variation_id='V1',active=True,is_default_vendor=True,unit_cost='0',pack_size=5)],
        purchase_orders=[dict(id=129,status='IN_TRANSIT')])


def test_repeat_order_independent_and_shared_product_distinct_variations():
    values = source()
    first = build_plan(**values).data
    for key in ('catalog','vendors','configs','purchase_orders'):
        values[key].reverse()
    assert build_plan(**values).data == first
    assert first['summary']['products'] == 1 and first['summary']['variations'] == 2
    assert first['variations'][0]['id'] != first['variations'][1]['id']
    assert [r['code'] for r in first['exceptions']] == ['DUPLICATE_SKU']
    assert first['candidates'][0]['disposition'] == 'UNAMBIGUOUS_EVIDENCE'
    assert 'assignments' not in first


def test_rename_keeps_ids_and_retains_new_provenance():
    a = source(); b = copy.deepcopy(a)
    b['catalog'][0]['sku']='NEW'; b['catalog'][0]['item_name']='Rename'
    pa,pb = build_plan(**a).data,build_plan(**b).data
    assert pa['variations'] == pb['variations'] and pa['products'] == pb['products']
    assert pa['run_id'] != pb['run_id']


def test_missing_parent_and_bad_variation_never_fabricate_references():
    values = source()
    values['catalog'][0]['square_item_id']=None
    values['catalog'][1]['square_variation_id']='SKU::not-square'
    plan=build_plan(**values).data
    assert plan['products'] == [] and len(plan['variations']) == 1
    assert plan['variations'][0]['product_id'] is None
    assert {'MISSING_ITEM_REFERENCE','INVALID_VARIATION_REFERENCE'} <= {r['code'] for r in plan['exceptions']}


def test_primary_and_supplier_external_conflicts_are_reported_not_chosen():
    values=source()
    values['vendors'][1]['square_vendor_id']='S1'
    values['configs'].append(dict(id=2,vendor_id=2,square_variation_id='V1',active=True,
                                 is_default_vendor=True,unit_cost=None,pack_size=None))
    plan=build_plan(**values).data
    assert all(r['square_vendor_id'] is None for r in plan['suppliers'])
    assert all(r['disposition']=='REVIEW_REQUIRED' for r in plan['candidates'])
    assert {'DUPLICATE_SUPPLIER_EXTERNAL_REFERENCE','CONFLICTING_PRIMARY_SUPPLIERS',
            'UNKNOWN_OR_INVALID_EXPECTED_COST','UNKNOWN_OR_INVALID_PACK_SIZE'} <= {r['code'] for r in plan['exceptions']}


def test_missing_config_vendor_does_not_create_supplier():
    values=source();values['configs'][0]['vendor_id']=999
    plan=build_plan(**values).data
    assert len(plan['suppliers']) == 2
    assert plan['candidates'][0]['supplier_id'] is None
    assert any(r['code']=='MISSING_CONFIG_SUPPLIER' for r in plan['exceptions'])


def test_plan_is_immutable_and_duplicate_source_claims_fail_closed():
    values=source();plan=build_plan(**values)
    data=plan.data;data['variations'].clear()
    assert len(plan.data['variations']) == 2
    values['catalog'].append(dict(values['catalog'][0], square_item_id='OTHER'))
    with pytest.raises(ValueError,match='Duplicate source row'):
        build_plan(**values)


def test_source_namespace_is_not_sku_or_name_based():
    assert identity('a','variation','v') != identity('b','variation','v')
    values=source();values['source_namespace']=''
    with pytest.raises(ValueError):build_plan(**values)
