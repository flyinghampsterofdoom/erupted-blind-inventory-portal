from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from app.config import settings


class StorageUnavailable(RuntimeError):
    pass


class SignageObjectStorage(Protocol):
    def put(self, key: str, content: bytes, *, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...


@dataclass
class R2ObjectStorage:
    endpoint_url: str
    bucket_name: str
    access_key_id: str
    secret_access_key: str
    region: str = 'auto'

    def _client(self):
        import boto3

        return boto3.client(
            's3',
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region or 'auto',
        )

    def put(self, key: str, content: bytes, *, content_type: str) -> None:
        self._client().put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=content,
            ContentType=content_type,
        )

    def get(self, key: str) -> bytes:
        response = self._client().get_object(Bucket=self.bucket_name, Key=key)
        return response['Body'].read()


class InMemorySignageObjectStorage:
    """Deterministic test adapter; never selected automatically by application code."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.put_count = 0
        self._lock = Lock()

    def put(self, key: str, content: bytes, *, content_type: str) -> None:
        with self._lock:
            if key not in self.objects:
                self.objects[key] = (bytes(content), content_type)
                self.put_count += 1

    def get(self, key: str) -> bytes:
        return self.objects[key][0]


def configured_signage_storage() -> SignageObjectStorage:
    values = (
        settings.r2_endpoint_url,
        settings.r2_bucket_name,
        settings.r2_access_key_id,
        settings.r2_secret_access_key,
    )
    if not all(str(value or '').strip() for value in values):
        raise StorageUnavailable(
            'Durable Digital Signage storage is not configured. Configure the private R2 bucket before uploading media.'
        )
    return R2ObjectStorage(
        endpoint_url=str(settings.r2_endpoint_url),
        bucket_name=str(settings.r2_bucket_name),
        access_key_id=str(settings.r2_access_key_id),
        secret_access_key=str(settings.r2_secret_access_key),
        region=str(settings.r2_region or 'auto'),
    )
