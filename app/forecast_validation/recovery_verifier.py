"""Owner-controlled local restoration; never imported by hosted execution."""
from pathlib import Path
import base64,hashlib,os
from datetime import datetime,timezone
from .contracts import EvidenceError,canonical,digest


def verify_locally(request, archive, identity, *, signing_key, admin_url,database_name,age,pg_restore='pg_restore'):
    if os.environ.get('RENDER') or os.environ.get('RENDER_SERVICE_ID'):
        raise EvidenceError('Private recovery verification must remain on the Owner-controlled host')
    from .public_backup import restore_public_local,_private_file
    from .backup import inventory
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    _private_file(signing_key)
    h=hashlib.sha256()
    with Path(archive).open('rb') as f:
        for block in iter(lambda:f.read(65536),b''):h.update(block)
    if h.hexdigest()!=request['encrypted_sha256']:raise EvidenceError('Downloaded archive differs')
    restored=restore_public_local(archive,identity,admin_url,database_name,age=age,pg_restore=pg_restore)
    try:actual=inventory(restored)
    finally:restored.engine.dispose()
    if len(actual)!=14 or digest(actual)!=request['inventory_sha256']:
        raise EvidenceError('Independent restored table contents differ')
    payload={'schema':'fv-independent-restore-v1','request_id':request['request_id'],
             'encrypted_sha256':request['encrypted_sha256'],'recipient':request['recipient'],
             'inventory_sha256':request['inventory_sha256'],'restored_inventory_sha256':digest(actual),
             'tables':14,'all_tables_equal':True,'independent_key_recovery_verified':True,
             'verified_at':datetime.now(timezone.utc).isoformat()}
    signer=Ed25519PrivateKey.from_private_bytes(Path(signing_key).read_bytes())
    return {'receipt':payload,'signature':base64.b64encode(signer.sign(canonical(payload).encode())).decode()}
