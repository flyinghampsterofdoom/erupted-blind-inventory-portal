"""Test-only confinement for the two explicitly authorized hosted R2 checks.

Application logical keys stay unchanged for format/association validation. Every
provider request is translated to a new verification namespace before signing.
No list/read/write/delete of existing application objects is exposed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings
from app.services import purchase_order_pdf_storage as pdfs
from app.services.digital_signage_storage import R2ObjectStorage

PREFIX_PATTERN = re.compile(r'verification/purchasing-v3/[0-9a-f]{32}\Z')


class ConfinedClient:
    def __init__(self, client, bucket, prefix, journal):
        self.client, self.bucket, self.prefix, self.journal = client, bucket, prefix, journal

    def key(self, logical):
        if logical.startswith(pdfs.PREFIX):
            name = pdfs._name(logical)
            area = 'pdf'
        elif re.fullmatch(r'digital-signage/images/[0-9a-f]{64}', logical):
            name, area = logical.rsplit('/', 1)[1], 'signage'
        else:
            raise AssertionError('Hosted test attempted an unexpected logical key')
        return self.prefix + '/' + area + '/' + name

    def _call(self, method, kwargs):
        assert kwargs.get('Bucket') == self.bucket
        logical = kwargs['Key']
        if method == 'put_object' and logical.startswith(pdfs.PREFIX):
            assert kwargs.get('IfNoneMatch') == '*', 'PDF writes must be conditional'
        actual = self.key(logical)
        assert actual.startswith(self.prefix + '/') and not actual.startswith(pdfs.PREFIX)
        event = {'operation': method, 'key': actual}
        try:
            result = getattr(self.client, method)(**{**kwargs, 'Key': actual})
        except ClientError as exc:
            event.update(status=exc.response['ResponseMetadata']['HTTPStatusCode'], error=exc.response['Error']['Code'], error_message=exc.response['Error'].get('Message',''))
            raise
        else:
            event['status'] = result['ResponseMetadata']['HTTPStatusCode']
            return result
        finally:
            self.journal.append(event)

    def put_object(self, **kwargs): return self._call('put_object', kwargs)
    def get_object(self, **kwargs): return self._call('get_object', kwargs)


class ConfinedSignageStorage:
    def __init__(self, client): self.client = client
    def put(self, key, content, *, content_type):
        self.client.put_object(Bucket=self.client.bucket, Key=key, Body=content, ContentType=content_type)
    def get(self, key):
        response = self.client.get_object(Bucket=self.client.bucket, Key=key)
        with response['Body'] as stream: return stream.read()


def configured_hosted_storage(monkeypatch, tmp_path):
    prefix = os.environ.get('HOSTED_R2_TEST_PREFIX', '')
    assert PREFIX_PATTERN.fullmatch(prefix), 'A unique verification/purchasing-v3/<UUID hex> prefix is required'
    prefix += '/' + uuid4().hex
    values = {name: os.environ.get('PO_PDF_TEST_R2_' + name.upper(), '')
              for name in ('endpoint_url', 'bucket_name', 'access_key_id', 'secret_access_key')}
    assert all(values.values()), 'Explicit hosted-test R2 credentials are required'
    assert values['endpoint_url'].startswith('https://'), 'Hosted probes require HTTPS'
    storage = R2ObjectStorage(**values, region='auto')
    raw = storage._client()
    # Listing is restricted to the newly authorized namespace; never inventory
    # existing media or historical PO objects for this test.
    initial = raw.list_objects_v2(Bucket=values['bucket_name'], Prefix=prefix+'/', MaxKeys=1)
    assert initial.get('KeyCount', 0) == 0, 'Test prefix must start empty'
    journal = []
    client = ConfinedClient(raw, values['bucket_name'], prefix, journal)
    anonymous = boto3.client('s3', endpoint_url=values['endpoint_url'], region_name='auto',
                             config=Config(signature_version=UNSIGNED, connect_timeout=8, read_timeout=15,
                                           retries={'max_attempts': 1}))
    unsigned = ConfinedClient(anonymous, values['bucket_name'], prefix, journal)
    media = ConfinedSignageStorage(client)
    # All application calls, including authenticated display retrieval, pass
    # through the confined real client. No production key can reach the SDK.
    monkeypatch.setattr(pdfs, '_r2', lambda: (client, values['bucket_name']))
    monkeypatch.setattr(settings, 'purchase_order_pdf_storage', 'r2')
    def report(name, checks):
        output = os.environ.get('HOSTED_R2_EVIDENCE_DIR')
        if output:
            target = Path(output); target.mkdir(parents=True, exist_ok=True)
            (target / (name+'.json')).write_text(json.dumps({'prefix':prefix, 'checks':checks, 'requests':journal}, indent=2)+'\n')
    return SimpleNamespace(client=client, unsigned=unsigned, media=media, bucket=values['bucket_name'],
                           prefix=prefix, journal=journal, raw=raw, report=report)


def assert_anonymous_denied(error):
    response = error.response
    status = response['ResponseMetadata']['HTTPStatusCode']
    detail = response['Error']
    # R2's S3 endpoint rejects an entirely absent Authorization header with this
    # exact 400 response; unrelated 400 errors are not evidence of privacy.
    assert (status == 403 and detail['Code'] == 'AccessDenied') or (
        status == 400 and detail['Code'] == 'InvalidArgument' and detail.get('Message') == 'Authorization')
    return {'status': status, 'error': detail['Code'], 'message': detail.get('Message', '')}
