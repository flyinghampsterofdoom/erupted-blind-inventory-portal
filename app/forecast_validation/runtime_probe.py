"""Public-verification-key protected telemetry, disabled when no key is pinned.

No business/evidence connection, source access, writes or background sampler.
The independent controller owns the signing key; Render holds only its public key.
"""
import base64,os,sys
from datetime import datetime,timezone
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from .contracts import EvidenceError,canonical


def authorize_probe(public_key, timestamp, signature, *, now=None):
    if not public_key:raise EvidenceError('Runtime monitoring is not commissioned')
    current=now or datetime.now(timezone.utc)
    try:
        ts=int(timestamp)
        if abs(current.timestamp()-ts)>30:raise ValueError()
        body=canonical({'purpose':'fv-runtime-read-v1','timestamp':ts}).encode()
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key,validate=True)).verify(
            base64.b64decode(signature,validate=True),body)
    except (ValueError,TypeError,InvalidSignature):raise EvidenceError('Invalid runtime observer authorization') from None


def measure():
    if sys.platform!='linux':raise EvidenceError('Linux cgroup runtime telemetry unavailable')
    root=Path('/sys/fs/cgroup')
    cpu=dict(row.split() for row in (root/'cpu.stat').read_text().splitlines())
    return {'at':datetime.now(timezone.utc).isoformat(),'memory_bytes':int((root/'memory.current').read_text()),
            'cpu_usage_seconds':int(cpu['usage_usec'])/1e6,
            'instance_id':os.environ.get('RENDER_INSTANCE_ID','local-runtime'),
            'revision':os.environ.get('RENDER_GIT_COMMIT','local'),
            'scope':'web cgroup only; no worker CPU is subtracted'}
