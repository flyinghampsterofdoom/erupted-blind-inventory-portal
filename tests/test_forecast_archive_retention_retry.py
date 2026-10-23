"""Actual R2 retention responses must preserve full remote-content verification."""
from hashlib import sha256
from io import BytesIO
import pytest
from botocore.exceptions import ClientError
from app.forecast_validation.backup import archive_to_s3
from app.forecast_validation.contracts import EvidenceError

@pytest.mark.parametrize('code',['PreconditionFailed','412','ObjectLockedByBucketPolicy'])
@pytest.mark.parametrize('remote',[b'matching ciphertext',b'different ciphertext',b'matching ciphertext!'])
def test_locked_retry_requires_exact_remote_content(tmp_path,code,remote):
    original=b'matching ciphertext';path=tmp_path/'archive';path.write_bytes(original)
    class Archive:
        def put_object(self,**kw):
            assert kw['IfNoneMatch']=='*'
            raise ClientError({'Error':{'Code':code}},'PutObject')
        def get_object(self,**kw):return {'Body':BytesIO(remote)}
    args=(Archive(),'dedicated-test-bucket',path,{'verified':True,'encrypted_sha256':sha256(original).hexdigest(),'encrypted_bytes':len(original)})
    if remote==original:
        assert archive_to_s3(*args,period_id='a'*64,custody={'verified':True})['off_device_copy_verified']
    else:
        with pytest.raises(EvidenceError,match='differs'):archive_to_s3(*args,period_id='a'*64,custody={'verified':True})

@pytest.mark.parametrize('code',['AccessDenied','InvalidAccessKeyId','InternalError'])
def test_other_upload_errors_never_become_success(tmp_path,code):
    path=tmp_path/'archive';path.write_bytes(b'x')
    class Archive:
        def put_object(self,**kw):raise ClientError({'Error':{'Code':code}},'PutObject')
        def get_object(self,**kw):pytest.fail('Unrelated upload error was suppressed')
    with pytest.raises(ClientError):archive_to_s3(Archive(),'dedicated-test-bucket',path,{'verified':True,'encrypted_sha256':sha256(b'x').hexdigest(),'encrypted_bytes':1},period_id='a'*64,custody={'verified':True})
