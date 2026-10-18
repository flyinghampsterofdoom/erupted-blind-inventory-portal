#!/usr/bin/env python3
"""Explicit PO-file backup and isolated restore; never writes databases or R2.

python -m scripts.purchase_order_pdf_backup backup references.json backup.zip
python -m scripts.purchase_order_pdf_backup verify backup.zip
python -m scripts.purchase_order_pdf_backup restore backup.zip /empty/isolated/path
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import zipfile

from app.services.purchase_order_pdf_storage import OriginalPdfUnavailable, read_pdf, validate_pdf, _name

ROOT = Path(__file__).resolve().parents[1]


def backup(references: list[str], destination: Path) -> dict:
    manifest = {'version': 1, 'documents': []}
    # Exclusive creation: a retry cannot overwrite an earlier backup.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for reference in sorted(set(references)):
            record = {'reference': reference}
            try:
                content = read_pdf(reference, root=ROOT).read_bytes()
            except OriginalPdfUnavailable as exc:
                record.update(status='unavailable', reason=exc.reason, original_file_backed_up=False)
            else:
                archive.writestr(reference, content)
                record.update(status='available', original_file_backed_up=True,
                              size=len(content), sha256=hashlib.sha256(content).hexdigest())
            manifest['documents'].append(record)
        archive.writestr('manifest.json', json.dumps(manifest, indent=2))
    with destination.open('rb') as stream:
        os.fsync(stream.fileno())
    verify(destination)
    return manifest


def verify(source: Path) -> dict:
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('version') != 1:
            raise ValueError('Unknown backup format')
        expected = {'manifest.json'}
        references = set()
        for record in manifest['documents']:
            reference = record['reference']
            _name(reference)
            if reference in references:
                raise ValueError('Duplicate backup reference')
            references.add(reference)
            if record['status'] == 'unavailable' and record['original_file_backed_up'] is False:
                continue
            if record['status'] != 'available' or record['original_file_backed_up'] is not True:
                raise ValueError('Invalid backup status')
            expected.add(reference)
            content = archive.read(reference)
            validate_pdf(reference, content)
            if len(content) != record['size'] or hashlib.sha256(content).hexdigest() != record['sha256']:
                raise ValueError('Backup checksum mismatch')
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError('Unexpected or duplicate backup members')
    return manifest


def restore(source: Path, destination: Path) -> dict:
    # Validate every byte before writing anything. Never overlay a live directory.
    manifest = verify(source)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    with zipfile.ZipFile(source) as archive:
        for record in manifest['documents']:
            if record['status'] != 'available':
                continue
            reference = record['reference']
            target = destination / reference
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(archive.read(reference))
            validate_pdf(reference, target.read_bytes())
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    command = sub.add_parser('backup')
    command.add_argument('references', type=Path)
    command.add_argument('destination', type=Path)
    command = sub.add_parser('verify')
    command.add_argument('source', type=Path)
    command = sub.add_parser('restore')
    command.add_argument('source', type=Path)
    command.add_argument('destination', type=Path)
    args = parser.parse_args()
    if args.operation == 'backup':
        # JSON list of exact DB paths. Export it using a read-only query; the tool
        # has no database connection, and cannot repair or rewrite a PO.
        result = backup(json.loads(args.references.read_text()), args.destination)
    elif args.operation == 'verify':
        result = verify(args.source)
    else:
        result = restore(args.source, args.destination)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
