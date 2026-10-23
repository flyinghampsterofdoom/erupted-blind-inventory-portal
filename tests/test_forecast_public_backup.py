"""Real age interoperability plus bounded subprocess and recovery failure tests."""
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import pytest
from app.forecast_validation import public_backup as public
from app.forecast_validation.backup import archive_to_s3
from app.forecast_validation.contracts import EvidenceError

@pytest.fixture
def crypto(tmp_path):
    age=os.environ.get('TEST_AGE_BINARY')
    if not age:pytest.skip('Explicit official age executable required')
    key=tmp_path/'identity';keygen=str(Path(age).with_name('age-keygen'))
    result=subprocess.run([keygen,'-o',str(key)],capture_output=True,text=True,check=True)
    key.chmod(0o600)
    recipient=result.stderr.strip().split('Public key: ')[-1]
    producer=tmp_path/'pg_dump'
    producer.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(bytes(range(256))*8192)\n');producer.chmod(0o700)
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,*a):return SimpleNamespace(scalars=lambda:SimpleNamespace(all=lambda:['fv_artifacts']))
    store=SimpleNamespace(url='postgresql://localhost/disposable',engine=SimpleNamespace(connect=Connection,dispose=lambda:None))
    return SimpleNamespace(age=age,key=key,recipient=recipient,producer=producer,store=store)

def export(crypto,path,**kw):return public.export_public(crypto.store,path,crypto.recipient,age=crypto.age,pg_dump=str(crypto.producer),**kw)

def test_real_age_public_only_export_and_owner_recovery(crypto,tmp_path):
    archive=tmp_path/'backup.age';receipt=export(crypto,archive)
    assert archive.stat().st_mode&0o077==0 and receipt['private_identity_required'] is False
    assert receipt['encrypted_sha256']==sha256(archive.read_bytes()).hexdigest()
    with public.decrypted_public(archive,crypto.key,age=crypto.age) as dump:
        assert dump.read_bytes()==bytes(range(256))*8192
    assert not dump.exists()
    class S3:
        def put_object(self,**kw):assert kw['Key'].endswith('.age') and kw['IfNoneMatch']=='*'
        def get_object(self,**kw):return {'Body':BytesIO(archive.read_bytes())}
    assert archive_to_s3(S3(),'dedicated-test',archive,receipt,period_id='a'*64,custody={'Owner_attested':True})['off_device_copy_verified']

@pytest.mark.parametrize('change',['corrupt','truncate'])
def test_authentication_failure_never_reaches_local_database(crypto,tmp_path,monkeypatch,change):
    archive=tmp_path/'backup.age';export(crypto,archive)
    data=bytearray(archive.read_bytes())
    if change=='corrupt':data[-80]^=1
    else:data=data[:-1]
    archive.write_bytes(data)
    monkeypatch.setattr(public,'_restore_dump_local',lambda *a,**k:pytest.fail('Unauthenticated archive touched database'))
    with pytest.raises(EvidenceError,match='authentication'):
        public.restore_public_local(archive,crypto.key,'postgresql://127.0.0.1/postgres','erupted_fv_restore_test',age=crypto.age)

@pytest.mark.parametrize('script,kwargs,match',[
 ('import sys;sys.exit(2)',{},'subprocess failed'),
 ('import time;time.sleep(5)',{'timeout_seconds':.1},'timeout'),
 ('import sys;sys.stdout.buffer.write(b"x"*200000)',{},'size bound'),
])
def test_failed_timeout_and_oversize_exports_are_removed(crypto,tmp_path,monkeypatch,script,kwargs,match):
    if match=='size bound':monkeypatch.setattr(public,'MAX_BACKUP',1000)
    crypto.producer.write_text('#!/usr/bin/env python3\n'+script+'\n')
    archive=tmp_path/'backup.age'
    with pytest.raises(EvidenceError,match=match):export(crypto,archive,**kwargs)
    assert not archive.exists()

def test_existing_artifact_and_insecure_identity_are_preserved(crypto,tmp_path):
    archive=tmp_path/'backup.age';archive.write_bytes(b'existing history')
    with pytest.raises(FileExistsError):export(crypto,archive)
    assert archive.read_bytes()==b'existing history'
    crypto.key.chmod(0o644)
    with pytest.raises(EvidenceError,match='private Owner file'):
        with public.decrypted_public(archive,crypto.key,age=crypto.age):pytest.fail('Insecure key accepted')

@pytest.mark.parametrize('url,name',[
 ('postgresql://production.example/production','erupted_fv_restore_test'),
 ('postgresql://127.0.0.1/postgres','existing_business'),
])
def test_nonlocal_or_existing_business_names_rejected(tmp_path,url,name):
    with pytest.raises(EvidenceError,match='fresh explicitly named local'):
        public.restore_public_local(tmp_path/'missing',tmp_path/'missing',url,name,age='unused')


def test_public_custody_requires_matching_recipient_and_recovery(crypto,tmp_path):
    import json
    receipt=tmp_path/'custody.json';data={'recipient':crypto.recipient,'custody_verified':True,'local_matching_identity_roundtrip_verified':True,'custodian':'test Owner','verified_at':'2026-10-23T00:00:00Z'}
    receipt.write_text(json.dumps(data));receipt.chmod(0o600)
    assert public.verify_public_custody(receipt,crypto.recipient)['recipient']==crypto.recipient
    data['local_matching_identity_roundtrip_verified']=False;receipt.write_text(json.dumps(data))
    with pytest.raises(EvidenceError,match='custody'):public.verify_public_custody(receipt,crypto.recipient)
    data['local_matching_identity_roundtrip_verified']=True;receipt.write_text(json.dumps(data))
    with pytest.raises(EvidenceError,match='custody'):public.verify_public_custody(receipt,'different')


@pytest.mark.parametrize('command',['weekly','capture','reconcile'])
def test_new_backup_options_do_not_bypass_disabled_worker(monkeypatch,command):
    import sys
    from app.forecast_validation import cli
    monkeypatch.setattr(cli.settings,'forecast_validation_worker_enabled',False)
    monkeypatch.setattr(cli,'Store',lambda *a,**k:pytest.fail('Disabled worker opened a database'))
    monkeypatch.setattr(sys,'argv',['forecast',command,'--backup-public-recipient','age1test','--age-binary','/unused'])
    with pytest.raises(EvidenceError,match='worker is disabled'):cli.main()
