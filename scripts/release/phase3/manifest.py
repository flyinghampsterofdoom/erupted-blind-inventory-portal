"""Content-addressed source release, using the existing clean-build attestation."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory
from scripts.release.build_identity import build
from scripts.release.workspace_evidence import git, runtime_path

VERSION = 'erupted-release-v1'
SUITE = 'erupted-verification-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(value, length=40):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{'+str(length)+'}', value):
        raise ValueError('Full immutable identity required')
    return value


def identity_at(root, revision):
    sha(revision)
    names = git(root, 'ls-tree', '-r', '--name-only', '-z', revision).decode().split('\0')[:-1]
    files = {p: hashlib.sha256(git(root, 'show', revision+':'+p)).hexdigest()
             for p in names if runtime_path(p)}
    return {'revision': revision, 'source_tree_sha256': digest(files), 'files': files}


def archive(root, revision):
    return git(root, 'archive', '--format=tar', sha(revision))


def seal(value):
    return {**value, 'manifest_id': digest(value)}


def validate(m):
    if not isinstance(m, dict) or m.get('format') != VERSION:
        raise ValueError('Unsupported release manifest')
    if m.get('manifest_id') != digest({k:v for k,v in m.items() if k != 'manifest_id'}):
        raise ValueError('Manifest content changed after sealing')
    sha(m['revision']); sha(m['source_tree_sha256'], 64)
    sha(m['artifact']['sha256'], 64)
    if m['artifact']['format'] != 'git-archive-tar' or m['artifact']['revision'] != m['revision']:
        raise ValueError('Artifact/source mismatch')
    if m['verification']['version'] != SUITE:
        raise ValueError('Unsupported verification suite')
    sha(m['verification']['sha256'], 64)
    if set(m['lockfiles']) != {'requirements.txt','requirements-test.txt'}:
        raise ValueError('Dependency locks absent/unsupported')
    for v in m['lockfiles'].values(): sha(v, 64)
    if set(m['roles']) != {'web', 'cron'} or any(m['roles'][r]['revision'] != m['revision'] or
            m['roles'][r]['source_tree_sha256'] != m['source_tree_sha256'] for r in m['roles']):
        raise ValueError('Web/cron candidate substitution')
    if m['schema']['expected'] not in m['schema']['candidate_supports']:
        raise ValueError('Candidate cannot run expected schema')
    if not isinstance(m['schema']['required_migrations'], list):
        raise ValueError('Explicit migration path required')
    if bool(m['schema']['required_migrations']) != (m['schema']['from'] != m['schema']['expected']):
        raise ValueError('Inconsistent migration requirements')
    if m['schema']['required_migrations'] and m['schema']['required_migrations'][-1] != m['schema']['expected']:
        raise ValueError('Migration path does not reach expected schema')
    if type(m['maintenance_required']) is not bool or (m['schema']['required_migrations'] and not m['maintenance_required']):
        raise ValueError('Migration requires maintenance and cron fence')
    baseline = m['baseline']; sha(baseline['revision']); sha(baseline['source_tree_sha256'], 64); sha(baseline['artifact_sha256'],64)
    if m['backup_requirement'] != ('isolated-restore' if m['schema']['required_migrations'] else 'verified-recovery-point'):
        raise ValueError('Backup requirement does not match release risk')
    if not baseline['supports'] or not m['compatibility_review']:
        raise ValueError('Explicit reviewed recovery compatibility required')
    stamp = datetime.fromisoformat(m['built_at'].replace('Z','+00:00'))
    if stamp.tzinfo is None: raise ValueError('Build timestamp needs timezone')
    return m


def prepare(root, revision, *, from_schema, baseline_revision, baseline_supports,
            compatibility_review, maintenance=False):
    root = Path(root).resolve(); sha(revision)
    if git(root, 'rev-parse', 'HEAD').decode().strip() != revision or git(root, 'status', '--porcelain').strip():
        raise ValueError('Release requires the exact committed clean checkout')
    identity = build(root)  # Existing source-file attestation; no DB/network.
    config = Config(str(root/'alembic.ini')); config.set_main_option('script_location', str(root/'migrations'))
    scripts = ScriptDirectory.from_config(config); expected = scripts.get_current_head()
    if not expected: raise ValueError('A single migration head is required')
    migrations = [r.revision for r in reversed(list(scripts.iterate_revisions(expected, from_schema)))]
    locks = {p: hashlib.sha256((root/p).read_bytes()).hexdigest()
             for p in ('requirements.txt', 'requirements-test.txt')}
    verification_files = {p: h for p,h in identity['files'].items()
                          if p.startswith('scripts/release/phase3/') or p == 'app/routers/deployment_verification.py'}
    result = seal({'format': VERSION, 'revision': revision, 'source_tree_sha256': identity['source_tree_sha256'],
        'backup_requirement':'isolated-restore' if migrations else 'verified-recovery-point',
        'artifact': {'format':'git-archive-tar', 'revision': revision,
                     'sha256': hashlib.sha256(archive(root, revision)).hexdigest()},
        'lockfiles':locks, 'schema':{'from':from_schema, 'expected':expected,
            'candidate_supports':[expected], 'required_migrations':migrations},
        'roles':{r:{'revision':revision,'source_tree_sha256':identity['source_tree_sha256']} for r in ('web','cron')},
        'baseline':{k:v for k,v in identity_at(root, baseline_revision).items() if k != 'files'} | {'supports':list(baseline_supports), 'artifact_sha256':hashlib.sha256(archive(root,baseline_revision)).hexdigest()},
        'compatibility_review':compatibility_review, 'maintenance_required':maintenance or bool(migrations),
        'verification':{'version':SUITE,'sha256':digest(verification_files)},
        'built_at':datetime.now(timezone.utc).isoformat()})
    return validate(result)


def verify_source(root, m, artifact=None):
    validate(m); root=Path(root)
    actual = prepare(root, m['revision'], from_schema=m['schema']['from'],
        baseline_revision=m['baseline']['revision'], baseline_supports=m['baseline']['supports'],
        compatibility_review=m['compatibility_review'], maintenance=m['maintenance_required'])
    if {k:v for k,v in actual.items() if k not in ('built_at','manifest_id')} != {k:v for k,v in m.items() if k not in ('built_at','manifest_id')}:
        raise ValueError('Source/build no longer matches candidate')
    if artifact is not None and hashlib.sha256(Path(artifact).read_bytes()).hexdigest() != m['artifact']['sha256']:
        raise ValueError('Artifact changed after qualification')
    return True


def write_once(path, value):
    with open(path, 'x', opener=lambda p,f:__import__('os').open(p,f,0o600)) as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True)+'\n')
