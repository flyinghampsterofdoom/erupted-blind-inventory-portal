"""Canonical JSON in bounded, independently verifiable compressed chunks.

The logical SHA is exactly contracts.digest(payload), including legacy records.
Temporary disk, SQL parameters and decompression all have explicit limits. No
whole-document JSON string or compressed blob is constructed by the writer.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import io
import json
import math
import os
import tempfile
import time
import zlib

import ijson

from .contracts import EvidenceError

CHUNK_BYTES = 64 * 1024
MAX_COMPRESSED = CHUNK_BYTES + 1024
MAX_BYTES = 256 * 1024 * 1024
MAX_CHUNKS = MAX_BYTES // CHUNK_BYTES
MAX_SCALAR = 16 * 1024
CODEC = 'canonical-json-zlib-chunks-v1'


def _check_scalars(value, depth=0):
    if depth > 100:
        raise EvidenceError('Evidence nesting exceeds the storage bound')
    if isinstance(value, str):
        if len(value) > MAX_SCALAR:
            raise EvidenceError('Evidence scalar exceeds 16 KiB; no truncation permitted')
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_SCALAR:
                raise EvidenceError('Evidence requires bounded string object keys')
            _check_scalars(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_scalars(item, depth + 1)
    elif isinstance(value, int):
        if value.bit_length() > 4096:
            raise EvidenceError('Evidence integer exceeds storage bound')
    elif value is not None and not isinstance(value, (float, bool)):
        raise EvidenceError('Unsupported evidence scalar')


def canonical_parts(value):
    _check_scalars(value)
    encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'), allow_nan=False)
    for part in encoder.iterencode(value):
        # ensure_ascii=True makes the character and byte lengths identical.
        yield part.encode('ascii')


@dataclass
class Prepared:
    directory: Path
    sha256: str
    size: int
    count: int
    chunks_sha256: str
    seconds: float

    def chunks(self):
        with (self.directory / 'index.jsonl').open() as index:
            for line in index:
                row = json.loads(line)
                row['data'] = (self.directory / str(row['ordinal'])).read_bytes()
                yield row


@contextmanager
def prepare(value=None, *, parts=None):
    """Spool compressed chunks, retaining at most one bounded input fragment.

    parts is for a frozen canonical stream, e.g. an exported legacy record. Its
    caller must supply metadata and independently verify the expected SHA.
    """
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='fv-evidence-') as name:
        directory = Path(name)
        directory.chmod(0o700)
        logical = sha256()
        sequence = sha256()
        total = count = 0
        buffer = bytearray()
        with (directory / 'index.jsonl').open('w') as index:
            def flush():
                nonlocal count
                raw = bytes(buffer)
                data = zlib.compress(raw, 6)
                row = {'ordinal': count, 'id': sha256(raw).hexdigest(),
                       'raw_bytes': len(raw), 'compressed_sha256': sha256(data).hexdigest()}
                if len(data) > MAX_COMPRESSED or count >= MAX_CHUNKS:
                    raise EvidenceError('Evidence chunk bound exceeded')
                path = directory / str(count)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(data)
                line = json.dumps(row, sort_keys=True, separators=(',', ':'))
                index.write(line + '\n')
                sequence.update((line + '\n').encode())
                count += 1
                buffer.clear()

            for fragment in (parts if parts is not None else canonical_parts(value)):
                if len(fragment) > 128 * 1024:
                    raise EvidenceError('Unbounded serialized fragment refused')
                total += len(fragment)
                if total > MAX_BYTES:
                    raise EvidenceError('Evidence exceeds 256 MiB; no partial publication')
                logical.update(fragment)
                offset = 0
                while offset < len(fragment):
                    take = min(CHUNK_BYTES - len(buffer), len(fragment) - offset)
                    buffer.extend(fragment[offset:offset + take])
                    offset += take
                    if len(buffer) == CHUNK_BYTES:
                        flush()
            if buffer:
                flush()
        if not count:
            raise EvidenceError('Empty evidence stream')
        yield Prepared(directory, logical.hexdigest(), total, count,
                       sequence.hexdigest(), time.monotonic() - began)


def decompress(row):
    data = bytes(row['data'])
    if len(data) > MAX_COMPRESSED or sha256(data).hexdigest() != row['compressed_sha256']:
        raise EvidenceError('Compressed evidence integrity check failed')
    if not 0 < row['raw_bytes'] <= CHUNK_BYTES:
        raise EvidenceError('Invalid evidence chunk size')
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(data, CHUNK_BYTES + 1)
    except zlib.error as exc:
        raise EvidenceError('Invalid compressed evidence') from exc
    if (len(raw) != row['raw_bytes'] or decoder.unconsumed_tail or decoder.unused_data
            or not decoder.eof or sha256(raw).hexdigest() != row['id']):
        raise EvidenceError('Uncompressed evidence integrity check failed')
    return raw


class IteratorReader(io.RawIOBase):
    def __init__(self, parts):
        self.parts = iter(parts)
        self.pending = b''

    def readable(self):
        return True

    def readinto(self, target):
        while not self.pending:
            try:
                self.pending = next(self.parts)
            except StopIteration:
                return 0
        count = min(len(target), len(self.pending))
        target[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count

    def close(self):
        close = getattr(self.parts, 'close', None)
        if close:
            close()
        super().close()


def _numbers(value):
    # ijson preserves arbitrarily large integers; convert JSON decimals back to
    # stdlib-json floats in place, without making another object graph.
    if isinstance(value, dict):
        for key in value:
            value[key] = _numbers(value[key])
    elif isinstance(value, list):
        for index in range(len(value)):
            value[index] = _numbers(value[index])
    elif isinstance(value, Decimal):
        value = float(value)
        if not math.isfinite(value):
            raise EvidenceError('Nonfinite JSON evidence')
    return value


def materialize(parts):
    """Worker-only full logical object; no full intermediate JSON string.

    The returned object necessarily occupies its logical size. Dashboard reads
    use small projections instead. Persistence itself does not materialize it.
    """
    with io.BufferedReader(IteratorReader(parts), buffer_size=CHUNK_BYTES) as stream:
        objects = ijson.items(stream, '', use_float=False)
        try:
            value = next(objects)
        except (StopIteration, ijson.JSONError) as exc:
            raise EvidenceError('Invalid evidence JSON') from exc
        try:
            next(objects)
        except StopIteration:
            pass
        else:
            raise EvidenceError('Multiple evidence documents')
    return _numbers(value)
