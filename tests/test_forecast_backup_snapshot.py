"""Real local PostgreSQL/age: backup inventory and dump share one MVCC snapshot."""
from pathlib import Path
from uuid import uuid4
import os,subprocess,hashlib,base64
import pytest
from sqlalchemy import text
from app.forecast_validation.storage import Store
from app.forecast_validation.backup import inventory
from app.forecast_validation.public_backup import export_public,restore_public_local
from app.forecast_validation.recovery_verifier import verify_locally
from app.forecast_validation.backup_verification import verified_recovery
from app.forecast_validation.contracts import digest
from app.forecast_validation.runner import forecast
from test_forecast_validation import snapshot,NOW
from test_forecast_validation_integration import evidence_pg,database_template


def test_snapshot_inventory_matches_restore_despite_later_operational_receipt(evidence_pg,database_template,tmp_path):
 age=os.environ.get('TEST_AGE_BINARY')
 if not age:pytest.skip('Explicit official age executable required')
 identity=tmp_path/'synthetic-age-identity';agekeygen=str(Path(age).with_name('age-keygen'))
 subprocess.run([agekeygen,'-o',str(identity)],check=True,capture_output=True);identity.chmod(0o600)
 recipient=subprocess.check_output([agekeygen,'-y',str(identity)],text=True).strip()
 forecast(evidence_pg,snapshot(),now=NOW);other=Store(evidence_pg.url)
 archive=tmp_path/'snapshot.age';pg='/opt/homebrew/opt/postgresql@16/bin/'
 with evidence_pg.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
  with connection.begin():
   connection.exec_driver_sql('SET TRANSACTION READ ONLY');sid=connection.execute(text('SELECT pg_export_snapshot()')).scalar_one()
   before=inventory(evidence_pg,connection=connection)
   other.attempt('local-notification','QUEUED','synthetic concurrent receipt')
   transport=export_public(evidence_pg,archive,recipient,age=age,pg_dump=pg+'pg_dump',connection=connection,snapshot_id=sid)
 assert inventory(other)!=before
 admin,url,_=database_template;name='erupted_fv_restore_snapshot_'+uuid4().hex
 restored=restore_public_local(archive,identity,url.set(database='postgres'),name,age=age,pg_restore=pg+'pg_restore')
 try:assert inventory(restored)==before
 finally:
  restored.engine.dispose()
  with admin.connect() as c:c.execute(text('DROP DATABASE "'+name+'" WITH (FORCE)'))
 # A synthetic Owner-host signer proves the signed request/archive/inventory
 # binding. No actual Owner key, remote storage or production host is used.
 from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
 from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption
 signer=Ed25519PrivateKey.generate();keyfile=tmp_path/'synthetic-signing-key';keyfile.write_bytes(signer.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption()));keyfile.chmod(0o600)
 public=base64.b64encode(signer.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
 req={'request_id':'a'*64,'encrypted_sha256':transport['encrypted_sha256'],'recipient':recipient,'inventory_sha256':digest(before)}
 name='erupted_fv_restore_signed_'+uuid4().hex
 try:
  signed=verify_locally(req,archive,identity,signing_key=keyfile,admin_url=url.set(database='postgres'),database_name=name,age=age,pg_restore=pg+'pg_restore')
  complete=verified_recovery({**transport,'off_device_copy_verified':True,'inventory_sha256':digest(before)},signed,verifier_public_key=public,request_id=req['request_id'])
  assert complete['independent_restore_verified']
 finally:
  with admin.connect() as c:c.execute(text('DROP DATABASE IF EXISTS "'+name+'" WITH (FORCE)'))
  other.engine.dispose()
