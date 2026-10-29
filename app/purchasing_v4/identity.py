"""Deterministic identity planning. Square/vendor/SKU suggestions grant no authority."""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from uuid import UUID, uuid5


NAMESPACE = UUID('6a6ec0d7-8cf3-4c2b-a266-45566337d092')
TRANSFORM_VERSION = 'phase1-identity-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identity(namespace, kind, reference):
    return str(uuid5(NAMESPACE, canonical([namespace, kind, str(reference)])))


def valid_reference(value):
    return isinstance(value, str) and bool(value) and value == value.strip() and not value.startswith('SKU::')


@dataclass(frozen=True)
class IdentityPlan:
    document: str

    @property
    def data(self):
        return json.loads(self.document)


def build_plan(*, source_namespace, catalog, vendors, configs, purchase_orders):
    """Namespace is a local source scope, never a fabricated Square merchant ID.

    No name/SKU merge, supplier inference, cost write or purchasing operation.
    Every input row is preserved as a content-addressed provenance observation.
    """
    if not isinstance(source_namespace, str) or not source_namespace.strip():
        raise ValueError('An explicit stable source namespace is required')
    inputs = {name: sorted((json.loads(canonical(r)) for r in rows), key=canonical)
              for name, rows in [('catalog', catalog), ('vendors', vendors),
                                 ('configs', configs), ('purchase_orders', purchase_orders)]}
    source_digest = digest(inputs)
    run = identity(source_namespace, 'import', TRANSFORM_VERSION+':'+source_digest)
    result = dict(run_id=run, source_namespace=source_namespace, source_digest=source_digest,
                  transform_version=TRANSFORM_VERSION,
                  products=[], variations=[], suppliers=[], candidates=[], procurement=[],
                  observations=[], exceptions=[])

    def exception(code, table, key, **details):
        row = dict(code=code, source_table=table, source_key=str(key), details=details)
        if row not in result['exceptions']:
            result['exceptions'].append(row)

    for name, table, key in [('catalog', 'ordering_catalog_identity', 'square_variation_id'),
                             ('vendors', 'vendors', 'id'), ('configs', 'vendor_sku_configs', 'id'),
                             ('purchase_orders', 'purchase_orders', 'id')]:
        for row in inputs[name]:
            result['observations'].append(dict(source_table=table, source_key=str(row.get(key)),
                                              source_digest=digest(row), evidence=row))
    by_variation = defaultdict(list)
    skus = defaultdict(set)
    for row in inputs['catalog']:
        vid = row.get('square_variation_id')
        if not valid_reference(vid):
            exception('INVALID_VARIATION_REFERENCE', 'ordering_catalog_identity', vid)
            continue
        by_variation[vid].append(row)
        if row.get('sku'):
            skus[row['sku']].add(vid)
    products = set()
    mapped = {}
    for vid, rows in sorted(by_variation.items()):
        if len(rows) != 1:
            exception('DUPLICATE_VARIATION_CLAIM', 'ordering_catalog_identity', vid,
                      claims=len(rows))
            continue
        row = rows[0]
        item = row.get('square_item_id')
        pid = None
        if valid_reference(item):
            pid = identity(source_namespace, 'product', item)
            if item not in products:
                products.add(item)
                result['products'].append(dict(id=pid, square_item_id=item))
        else:
            exception('MISSING_ITEM_REFERENCE', 'ordering_catalog_identity', vid)
        variation = identity(source_namespace, 'variation', vid)
        result['variations'].append(dict(id=variation, product_id=pid, square_variation_id=vid))
        mapped[vid] = variation
    for sku, vids in sorted(skus.items()):
        if len(vids) > 1:
            exception('DUPLICATE_SKU', 'ordering_catalog_identity', sku, variation_ids=sorted(vids))

    supplier_claims = defaultdict(list)
    vendor_by_id = {}
    for vendor in inputs['vendors']:
        key = vendor.get('id')
        if type(key) is not int or key < 1 or key in vendor_by_id:
            raise ValueError('Vendor source requires unique positive legacy identities')
        vendor_by_id[key] = vendor
        if valid_reference(vendor.get('square_vendor_id')):
            supplier_claims[vendor['square_vendor_id']].append(key)
    suppliers = {}
    ambiguous_vendors = set()
    for key, vendor in sorted(vendor_by_id.items()):
        ref = vendor.get('square_vendor_id')
        if not valid_reference(ref):
            exception('MISSING_SUPPLIER_EXTERNAL_REFERENCE', 'vendors', key)
            ref = None
        elif len(supplier_claims[ref]) > 1:
            exception('DUPLICATE_SUPPLIER_EXTERNAL_REFERENCE', 'vendors', key,
                      vendor_ids=sorted(supplier_claims[ref]))
            ambiguous_vendors.add(key)
            ref = None
        sid = identity(source_namespace, 'supplier', key)
        suppliers[key] = sid
        result['suppliers'].append(dict(id=sid, legacy_vendor_id=key, square_vendor_id=ref))

    defaults = defaultdict(set)
    offer_details = defaultdict(set)
    for row in inputs['configs']:
        if row.get('active'):
            vid, vendor = row.get('square_variation_id'), row.get('vendor_id')
            if row.get('is_default_vendor'):
                defaults[vid].add(vendor)
            offer_details[(vid, vendor)].add(canonical([row.get('unit_cost'), row.get('pack_size')]))
    for row in inputs['configs']:
        key, vid, vendor = row['id'], row.get('square_variation_id'), row.get('vendor_id')
        problems = []
        if vid not in mapped:
            problems.append('UNRESOLVED_CONFIG_VARIATION')
        if vendor not in suppliers:
            problems.append('MISSING_CONFIG_SUPPLIER')
        elif not vendor_by_id[vendor].get('active') or vendor in ambiguous_vendors:
            problems.append('INACTIVE_OR_AMBIGUOUS_SUPPLIER')
        if len(defaults[vid]) > 1:
            problems.append('CONFLICTING_PRIMARY_SUPPLIERS')
        if len(offer_details[(vid, vendor)]) > 1:
            problems.append('CONFLICTING_PURCHASING_DETAILS')
        pack = row.get('pack_size')
        if type(pack) is not int or pack < 1:
            problems.append('UNKNOWN_OR_INVALID_PACK_SIZE')
        cost = row.get('unit_cost')
        try:
            if cost is None or not Decimal(str(cost)).is_finite() or Decimal(str(cost)) < 0:
                problems.append('UNKNOWN_OR_INVALID_EXPECTED_COST')
        except Exception:
            problems.append('UNKNOWN_OR_INVALID_EXPECTED_COST')
        for code in problems:
            labels = by_variation.get(vid, [])
            exception(code, 'vendor_sku_configs', key, square_variation_id=vid, legacy_vendor_id=vendor,
                      sku=row.get('sku'), item_name=labels[0].get('item_name') if len(labels)==1 else None)
        result['candidates'].append(dict(source_config_id=key, variation_id=mapped.get(vid),
            supplier_id=suppliers.get(vendor), explicit_default=bool(row.get('is_default_vendor')),
            disposition='REVIEW_REQUIRED' if problems or not row.get('active') else 'UNAMBIGUOUS_EVIDENCE'))
    for po in inputs['purchase_orders']:
        result['procurement'].append(dict(legacy_po_id=po['id'], original_digest=digest(po)))
    # Duplicate source keys cannot produce ambiguous provenance rows or guessed winners.
    observations = defaultdict(list)
    for row in result['observations']:
        observations[(row['source_table'], row['source_key'])].append(row)
    result['observations'] = []
    for (table, key), rows in sorted(observations.items()):
        if len(rows) > 1:
            raise ValueError(f'Duplicate source row identity: {table}/{key}')
        result['observations'].extend(rows)
    result['exceptions'].sort(key=lambda row: (row['code'], row['source_table'], row['source_key']))
    result['summary'] = {name: len(result[name]) for name in
                         ('products', 'variations', 'suppliers', 'candidates', 'procurement',
                          'observations', 'exceptions')}
    result['plan_digest'] = digest(result)
    return IdentityPlan(canonical(result))
