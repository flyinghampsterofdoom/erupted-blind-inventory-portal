"""A terminal log receipt supplements the existing transactional work records.

No extra table, migration, job, identity, or scheduler. A committed no-op is
distinct from generation; blocked/disabled results never prove expected work.
Provider logs must be retained with release evidence before their retention ends.
"""
from datetime import datetime, timezone


def receipt(identity, run_id, phase, *, schema=None, result=None, duration_ms=0):
    value = {'event': 'ERUPTED_CRON_EXECUTION_V1', 'run_id': run_id, 'phase': phase,
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'revision': identity.get('revision'), 'source_tree_sha256': identity.get('source_tree_sha256'),
        'identity_status': identity.get('status'), 'service_id': identity.get('service_id'),
        'schema': schema, 'duration_ms': duration_ms}
    if phase == 'committed' and isinstance(result, dict):
        counts = {name: len(result.get(name, [])) for name in
                  ('generated_period_ids', 'published_period_ids', 'blocked_period_ids')}
        value['counts'] = counts
        # The service's disabled-policy branch lacks `results`; success may mean
        # no week was missing. Do not conflate policy-disabled with healthy work.
        value['work'] = ('blocked' if counts['blocked_period_ids'] else
                         'policy-disabled' if 'results' not in result else
                         'generated' if counts['generated_period_ids'] else 'no-work-due')
    return value
