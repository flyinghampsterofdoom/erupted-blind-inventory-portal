"""Shared command contract for the finite history job and its rehearsal."""
import re
import shlex

ENTRYPOINT = ('sh', 'scripts/release/purchasing_history_job.sh')


def start_command(revision, digest, *, preflight=False):
    if not re.fullmatch('[0-9a-f]{40}', revision) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('History job requires the exact revision and source digest')
    return (f'ERUPTED_EXPECTED_REVISION={revision} ERUPTED_EXPECTED_SOURCE_SHA256={digest} '
            + shlex.join(ENTRYPOINT + (('--preflight',) if preflight else ())))
