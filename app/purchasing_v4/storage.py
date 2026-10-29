"""Append-only foundation importer. Writes only isolated purchasing_v4 tables."""
import json
from decimal import Decimal
from sqlalchemy import text
from .identity import canonical, identity, digest, build_plan
from .isolation import require_isolated


class IdentityCollisionError(ValueError):
    def __init__(self, table, keys):
        self.exception = dict(code='EXISTING_IDENTITY_CONFLICT', source_table=table, keys=keys)
        super().__init__(canonical(self.exception))


_COLUMNS = {
    'import_run': ('id', 'source_namespace', 'source_digest', 'transform_version', 'plan_digest', 'summary'),
    'product': ('id', 'created_by_run'),
    'variation': ('id', 'product_id', 'created_by_run'),
    'product_reference': ('source_namespace', 'square_item_id', 'product_id', 'created_by_run'),
    'variation_reference': ('source_namespace', 'square_variation_id', 'variation_id', 'created_by_run'),
    'supplier': ('id', 'created_by_run'),
    'supplier_reference': ('source_namespace', 'legacy_vendor_id', 'square_vendor_id', 'supplier_id', 'created_by_run'),
    'source_observation': ('run_id', 'source_table', 'source_key', 'source_digest', 'evidence'),
    'identity_exception': ('run_id', 'code', 'source_table', 'source_key', 'details'),
    'supplier_candidate': ('run_id', 'source_config_id', 'variation_id', 'supplier_id', 'explicit_default', 'disposition'),
    'legacy_procurement_reference': ('source_namespace', 'legacy_po_id', 'original_digest', 'created_by_run'),
}
_JSON = {'summary', 'evidence', 'details'}
_SOURCE_KEYS = {'ordering_catalog_identity': 'square_variation_id', 'vendors': 'id',
                'vendor_sku_configs': 'id', 'purchase_orders': 'id'}


def _insert(connection, table, values, keys, *, stable=None):
    columns = _COLUMNS[table]  # Identifiers are this static allowlist, never input.
    params = {name: canonical(values[name]) if name in _JSON else values[name] for name in columns}
    binds = [f'CAST(:{name} AS jsonb)' if name in _JSON else f':{name}' for name in columns]
    connection.execute(text(f'INSERT INTO purchasing_v4.{table} ({",".join(columns)}) '
                            f'VALUES ({",".join(binds)}) ON CONFLICT DO NOTHING'), params)
    predicate = ' AND '.join(f'{name} = :{name}' for name in keys)
    row = connection.execute(text(f'SELECT * FROM purchasing_v4.{table} WHERE {predicate}'),
                             {name: values[name] for name in keys}).mappings().one_or_none()
    stable = stable or [c for c in columns if c not in ('created_by_run',)]
    if row is None or any(canonical(row[name]) != canonical(values[name]) for name in stable):
        raise IdentityCollisionError(table, {name: str(values[name]) for name in keys})


def apply_plan(connection, plan):
    """Atomic replay-safe rehearsal. Caller owns outer transaction/commit.

    A SAVEPOINT guarantees that catching a conflict cannot leave half an import.
    There are no update/delete statements or public-schema writes in this module.
    """
    require_isolated(connection)
    data = plan.data
    ns, run = data['source_namespace'], data['run_id']
    if run != identity(ns, 'import', data['transform_version']+':'+data['source_digest']):
        raise ValueError('Plan run identity does not match its source digest')
    sources = {table: [] for table in _SOURCE_KEYS}
    for observation in data['observations']:
        if observation['source_table'] not in sources:
            raise ValueError('Unsupported provenance source')
        sources[observation['source_table']].append(observation['evidence'])
    rebuilt = build_plan(source_namespace=ns, catalog=sources['ordering_catalog_identity'],
        vendors=sources['vendors'], configs=sources['vendor_sku_configs'],
        purchase_orders=sources['purchase_orders'])
    if rebuilt.document != plan.document:
        raise ValueError('Plan does not match its deterministic provenance derivation')
    with connection.begin_nested():
        connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        connection.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))'),
                           {'scope': 'purchasing_v4:' + ns})
        existing = connection.execute(text('SELECT source_digest, transform_version, plan_digest, summary FROM purchasing_v4.import_run WHERE id=:id'),
                                      {'id': run}).mappings().one_or_none()
        if existing:
            if any(existing[key] != data[key] for key in ('source_digest','transform_version','plan_digest','summary')):
                raise IdentityCollisionError('import_run', {'id': run})
            return dict(run_id=run, replayed=True, summary=existing['summary'])
        # Snapshot references are checked against actual target source facts.
        # No cross-schema FK is added: an inactive evidence module must not
        # change legacy deletion/update behavior. These are provenance links,
        # not active procurement ownership or supplier assignments.
        for observation in data['observations']:
            table = observation['source_table']
            if table not in _SOURCE_KEYS:
                raise ValueError('Unsupported provenance source')
            key_column = _SOURCE_KEYS[table]
            key = observation['source_key']
            if key_column == 'id':
                key = int(key)
            actual = connection.execute(text(f'SELECT to_jsonb(t)::text FROM public.{table} t '
                                              f'WHERE {key_column}=:key'), {'key':key}).scalar_one_or_none()
            if actual is None or digest(json.loads(actual, parse_float=Decimal)) != observation['source_digest']:
                raise IdentityCollisionError(table, {'source_key':observation['source_key']})
        _insert(connection, 'import_run', dict(id=run, source_namespace=ns,
                source_digest=data['source_digest'], transform_version=data['transform_version'],
                plan_digest=data['plan_digest'], summary=data['summary']), ['id'])
        for row in data['products']:
            if row['id'] != identity(ns, 'product', row['square_item_id']):
                raise ValueError('Product identity does not match reference')
            _insert(connection, 'product', dict(id=row['id'], created_by_run=run), ['id'])
            _insert(connection, 'product_reference', dict(source_namespace=ns,
                    square_item_id=row['square_item_id'], product_id=row['id'], created_by_run=run),
                    ['source_namespace', 'square_item_id'])
        for row in data['variations']:
            if row['id'] != identity(ns, 'variation', row['square_variation_id']):
                raise ValueError('Variation identity does not match reference')
            _insert(connection, 'variation', dict(id=row['id'], product_id=row['product_id'],
                    created_by_run=run), ['id'])
            _insert(connection, 'variation_reference', dict(source_namespace=ns,
                    square_variation_id=row['square_variation_id'], variation_id=row['id'], created_by_run=run),
                    ['source_namespace', 'square_variation_id'])
        for row in data['suppliers']:
            if row['id'] != identity(ns, 'supplier', row['legacy_vendor_id']):
                raise ValueError('Supplier identity does not match real legacy vendor')
            _insert(connection, 'supplier', dict(id=row['id'], created_by_run=run), ['id'])
            _insert(connection, 'supplier_reference', dict(source_namespace=ns,
                    legacy_vendor_id=row['legacy_vendor_id'], square_vendor_id=row['square_vendor_id'],
                    supplier_id=row['id'], created_by_run=run), ['source_namespace', 'legacy_vendor_id'])
        for table, name in [('source_observation', 'observations'), ('identity_exception', 'exceptions'),
                             ('supplier_candidate', 'candidates')]:
            keys = {'source_observation': ['run_id', 'source_table', 'source_key'],
                    'identity_exception': ['run_id', 'code', 'source_table', 'source_key'],
                    'supplier_candidate': ['run_id', 'source_config_id']}[table]
            for row in data[name]:
                _insert(connection, table, dict(run_id=run, **row), keys)
        for row in data['procurement']:
            _insert(connection, 'legacy_procurement_reference', dict(source_namespace=ns,
                    created_by_run=run, **row), ['source_namespace', 'legacy_po_id'],
                    stable=['source_namespace', 'legacy_po_id'])
    return dict(run_id=run, replayed=False, summary=data['summary'])
