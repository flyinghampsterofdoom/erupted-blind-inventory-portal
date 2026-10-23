"""Separate transport and Owner-controlled restoration without downgrading COMPLETE."""
import base64
from datetime import datetime,timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature
from .contracts import EvidenceError,canonical,stamp


def verified_recovery(transport, signed, *, verifier_public_key, request_id, now=None):
    current=stamp(now or datetime.now(timezone.utc))
    try:
        payload=signed['receipt']
        Ed25519PublicKey.from_public_bytes(base64.b64decode(verifier_public_key,validate=True)).verify(
            base64.b64decode(signed['signature'],validate=True),canonical(payload).encode())
        age=(current-stamp(payload['verified_at'])).total_seconds()
        if not (payload['schema']=='fv-independent-restore-v1' and payload['request_id']==request_id
                and payload['encrypted_sha256']==transport['encrypted_sha256']
                and payload['recipient']==transport['recipient'] and 0<=age<=86400
                and payload['inventory_sha256']==transport['inventory_sha256']
                and payload['inventory_sha256']==payload['restored_inventory_sha256']
                and len(payload['inventory_sha256'])==64 and payload['tables']==14
                and payload['all_tables_equal'] is True and payload['independent_key_recovery_verified'] is True
                and transport['off_device_copy_verified'] is True):
            raise ValueError()
    except (ValueError,TypeError,KeyError,InvalidSignature):
        raise EvidenceError('Same-archive independent restoration is not verified') from None
    return {**transport,'verified':True,'independent_restore_verified':True,
            'restore_request_id':request_id,'restore_verified_at':payload['verified_at']}
