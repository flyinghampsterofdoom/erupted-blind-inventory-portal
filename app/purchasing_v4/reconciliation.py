"""Independent read-only preservation evidence; never restore/replay business facts."""
import hashlib
from sqlalchemy import text
from .identity import digest
from .isolation import require_read_only


def public_fingerprints(connection):
    require_read_only(connection)
    names = connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars()
    result = {}
    preparer = connection.dialect.identifier_preparer
    for name in list(names):
        # Names originate in pg_catalog and are quoted as identifiers.
        table = 'public.' + preparer.quote(name)
        checksum, count = hashlib.sha256(), 0
        rows = connection.execution_options(stream_results=True).execute(text(
            f'SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text'))
        for row in rows:
            value = row[0].encode()
            checksum.update(str(len(value)).encode()+b':'+value)
            count += 1
        rows.close()
        connection.execution_options(stream_results=False)
        result[name] = dict(rows=count, sha256=checksum.hexdigest())
    return result


def public_structure(connection):
    require_read_only(connection)
    rows = connection.execute(text('''SELECT table_name, column_name, data_type, udt_name,
        is_nullable, column_default, ordinal_position FROM information_schema.columns
        WHERE table_schema='public' ORDER BY table_name, ordinal_position''')).mappings().all()
    constraints = connection.execute(text('''SELECT c.relname, con.conname,
        pg_get_constraintdef(con.oid) AS definition FROM pg_constraint con
        JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' ORDER BY c.relname, con.conname''')).mappings().all()
    sequences = connection.execute(text('''SELECT schemaname, sequencename, last_value,
        start_value, increment_by FROM pg_sequences WHERE schemaname='public'
        ORDER BY sequencename''')).mappings().all()
    indexes = connection.execute(text('''SELECT tablename,indexname,indexdef FROM pg_indexes
        WHERE schemaname='public' ORDER BY tablename,indexname''')).mappings().all()
    triggers = connection.execute(text('''SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid) AS definition
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname''')).mappings().all()
    enums = connection.execute(text('''SELECT t.typname,e.enumlabel,e.enumsortorder
        FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname='public' ORDER BY t.typname,e.enumsortorder''')).mappings().all()
    return dict(columns_digest=digest([dict(r) for r in rows]),
                constraints_digest=digest([dict(r) for r in constraints]),
                sequences_digest=digest([dict(r) for r in sequences]),
                indexes_digest=digest([dict(r) for r in indexes]),
                triggers_digest=digest([dict(r) for r in triggers]),
                enums_digest=digest([dict(r) for r in enums]))
